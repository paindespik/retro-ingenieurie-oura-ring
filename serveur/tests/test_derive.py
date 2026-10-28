"""Tests des dérivations (bibliothèque standard uniquement).

    cd serveur && python3 -m unittest discover -s tests -v
"""
import json
import math
import os
import sqlite3
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

import derive_night as dn  # noqa: E402
import oura_core as oc  # noqa: E402
import staging  # noqa: E402
import synth  # noqa: E402


class Base(unittest.TestCase):
    nights = 5
    sick_last = True

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.raw = os.path.join(cls.tmp.name, "oura.db")
        cls.der = os.path.join(cls.tmp.name, "derived.db")
        cls.ring = synth.build(cls.raw, nights=cls.nights, sick_last=cls.sick_last)
        dn.run(cls.raw, cls.der, briefing=False, recompute_all=True, log=lambda *_: None)
        cls.con = sqlite3.connect(f"file:{cls.raw}?mode=ro", uri=True)
        d = sqlite3.connect(cls.der)
        d.row_factory = sqlite3.Row
        cls.rows = [dict(r) for r in d.execute("SELECT * FROM sleep_scores ORDER BY night")]
        cls.readiness = {r["day"]: dict(r) for r in d.execute("SELECT * FROM readiness")}
        cls.derived = d

    @classmethod
    def tearDownClass(cls):
        cls.con.close()
        cls.derived.close()
        cls.tmp.cleanup()


class TimeAxisTest(Base):
    def test_monotone_and_close_to_truth(self):
        ax = oc.TimeAxis(self.con)
        self.assertEqual(ax.mode, "sync")
        rts = [r for r, in self.con.execute("SELECT DISTINCT ring_timestamp FROM events ORDER BY 1")]
        ts = [ax(r) for r in rts]
        self.assertTrue(all(b >= a for a, b in zip(ts, ts[1:])), "axe non monotone")
        # ancres bruitées de ±120 s : l'ajustement robuste doit faire bien mieux
        err = [abs(ax(r) - self.ring.unix(r)) for r in rts[:: max(1, len(rts) // 500)]]
        self.assertLess(max(err), 60, f"erreur max {max(err):.0f} s")

    def test_inverse(self):
        ax = oc.TimeAxis(self.con)
        rt = self.ring.rt(self.ring.t0 + 3 * 86400)
        self.assertLess(abs(ax.to_ds(ax(rt)) - rt), 5)


class SignalTest(Base):
    def test_hrv_bins_end_aligned(self):
        ax = oc.TimeAxis(self.con)
        n = oc.load_nights(self.con, ax)[0]
        bins = oc.hrv_bins(self.con, n["start_ds"], n["end_ds"])
        self.assertTrue(bins)
        # le 1er bin couvre les 5 premières minutes : centre ≈ début + 150 s
        self.assertLess(abs(bins[0][0] - (n["start_ds"] + 1500)), 20)

    def test_beats_cumulative_timing(self):
        ax = oc.TimeAxis(self.con)
        n = oc.load_nights(self.con, ax)[0]
        beats = oc.load_beats(self.con, n["start_ds"], n["end_ds"])
        clean = oc.clean_beats(beats)
        self.assertGreater(len(clean) / len(beats), 0.95)
        gaps = [b[0] - a[0] for a, b in zip(beats, beats[1:])]
        self.assertGreater(min(gaps), 0, "battements non ordonnés (horodatage par paquet)")

    def test_resp_rate_recovered(self):
        ax = oc.TimeAxis(self.con)
        n = oc.load_nights(self.con, ax)[0]
        clean = oc.clean_beats(oc.load_beats(self.con, n["start_ds"], n["end_ds"]))
        rr = oc.resp_rate_windows(clean, n["start_ds"], n["end_ds"])
        self.assertGreater(len(rr), 20)
        med = sorted(v for _, v in rr)[len(rr) // 2]
        self.assertAlmostEqual(med, 14.0, delta=1.0)

    def test_skin_temp_uses_first_sensor_only(self):
        self.assertEqual(oc.skin_temp({"temps_c": [34.4, 36, 37.1]}), 34.4)
        self.assertIsNone(oc.skin_temp({"temps_c": [12.0, 36, 37]}))


class NightTest(Base):
    def test_nights_found(self):
        self.assertEqual(len(self.rows), self.nights)

    def test_physiological_architecture(self):
        for r in self.rows:
            sleep = r["deep"] + r["rem"] + r["light"]
            self.assertLessEqual(r["deep"] / sleep, staging.CAPS["deep"] + 0.01)
            self.assertLessEqual(r["rem"] / sleep, staging.CAPS["rem"] + 0.01)
            st = [s for s, in self.derived.execute(
                "SELECT stage FROM night_staging WHERE night=? ORDER BY epoch", (r["night"],))]
            trans = sum(1 for a, b in zip(st, st[1:]) if a != b)
            self.assertLess(trans, 150, "hypnogramme trop fragmenté")
            onset = next(i for i, s in enumerate(st) if s != "awake")
            self.assertNotIn("rem", st[onset:onset + 60], "REM dans les 30 premières minutes")

    def test_lowest_hr_definition(self):
        r = self.rows[0]
        self.assertTrue(55 <= r["hr_min"] <= 66, r["hr_min"])
        self.assertIsNotNone(r["hr_lowest_at"])
        # « hamac » : FC la plus basse dans la première moitié → récupération ≥ 4 h
        self.assertGreater(r["recovery_index"], 4)

    def test_temperature_baseline_no_leakage(self):
        first3 = self.rows[:3]
        self.assertTrue(all(r["temp_dev"] is None for r in first3), "écart avant 3 nuits")
        last = self.rows[-1]
        self.assertEqual(last["temp_status"], "provisoire")
        self.assertAlmostEqual(last["temp_dev"], 1.2, delta=0.2)
        # la nuit n-1 ne doit pas « voir » la nuit malade suivante
        self.assertLess(abs(self.rows[-2]["temp_dev"]), 0.2)

    def test_restfulness_monotone(self):
        a = dn.subscore_restfulness(10, 0, 0.02)
        b = dn.subscore_restfulness(40, 3, 0.10)
        c = dn.subscore_restfulness(90, 8, 0.30)
        self.assertGreater(a, b)
        self.assertGreater(b, c)

    def test_no_spo2_percentage(self):
        self.assertTrue(all(r["spo2"] is None for r in self.rows))

    def test_incremental_skip(self):
        logs = []
        dn.run(self.raw, self.der, log=logs.append)
        self.assertFalse([x for x in logs if "— score" in x], "nuits recalculées sans changement")


class RecoveryTest(Base):
    def test_tension_on_sick_night(self):
        last = self.rows[-1]["night"]
        t = json.loads(self.readiness[last]["tension"])
        self.assertEqual(t["level"], "marqués")
        metrics = {s["metric"] for s in t["signals"]}
        self.assertIn("hr_min", metrics)
        self.assertIn("temp_dev", metrics)

    def test_no_tension_on_normal_night(self):
        t = json.loads(self.readiness[self.rows[-2]["night"]]["tension"])
        self.assertEqual(t["level"], "aucun")

    def test_contributors_in_range(self):
        for r in self.readiness.values():
            for k, c in json.loads(r["contributors"]).items():
                if c.get("status") == "calibrage":                  # équilibre HRV avant 60 nuits
                    self.assertIsNone(c["score"])
                    continue
                self.assertTrue(0 <= c["score"] <= 100, (k, c))

    def test_briefing_number_guard(self):
        night = self.rows[-1]["night"]
        ctx = dn.briefing_context(self.derived, night)
        rec = json.loads(ctx)["recuperation_du_jour"]["score_recuperation"]
        sleep = json.loads(ctx)["nuit_a_resumer"]["score_sommeil"]
        good = f"Score de sommeil {sleep:.0f}, récupération {rec:.0f}. En cas d'urgence : 15 ou 112."
        self.assertEqual(dn.unverified_numbers(good, ctx), [])
        self.assertIn("987", dn.unverified_numbers("récupération 987", ctx))

    def test_model_choice(self):
        big, small = dn.LLM_MODEL, dn.LLM_MODEL_FALLBACK
        # mode « résident » (OURA_LLM_MAY_SWAP=0) : ne jamais décharger la 3090
        polite = {(big, small): big,                       # déjà chargé → gros modèle
                  (small,): big,                           # 3090 libre → gros modèle
                  ("gemma4-31b", small): small,            # 3090 occupée → 4B
                  None: small}                             # llama-swap illisible → 4B
        # mode « à la demande » (défaut depuis Ling) : la 3090 peut être reprise
        swap = {**polite, ("gemma4-31b", small): big}
        orig, orig_swap = dn.running_models, dn.LLM_MAY_SWAP
        try:
            for may, cases in ((False, polite), (True, swap)):
                dn.LLM_MAY_SWAP = may
                for run, want in cases.items():
                    dn.running_models = lambda r=run: list(r) if r else r
                    self.assertEqual(dn.choose_model(), want, (may, run))
        finally:
            dn.running_models, dn.LLM_MAY_SWAP = orig, orig_swap

    def test_llm_options_per_family(self):
        ling = dn.llm_options("ling-sante")
        self.assertEqual(ling["chat_template_kwargs"], {"enable_thinking": True})
        self.assertGreater(ling["thinking_budget_tokens"], 0)
        self.assertNotIn("reasoning_effort", ling["chat_template_kwargs"])
        self.assertEqual(dn.llm_options("qwen3.8-27b")["chat_template_kwargs"],
                         {"reasoning_effort": dn.LLM_REASONING})

    def test_briefing_hash_ignores_contributor_noise(self):
        night = self.rows[-1]["night"]
        ctx = json.loads(dn.briefing_context(self.derived, night))
        h = dn.briefing_hash(json.dumps(ctx))
        for k in (ctx["recuperation_du_jour"] or {}).get("contributeurs_sur_100", {}):
            ctx["recuperation_du_jour"]["contributeurs_sur_100"][k] = 1
        self.assertEqual(dn.briefing_hash(json.dumps(ctx)), h)
        ctx["journal"] = [{"jour": night, "type": "maladie", "note": None}]
        self.assertNotEqual(dn.briefing_hash(json.dumps(ctx)), h)
        self.assertIn("comparaison_a_vos_references", ctx)

    def test_lerp_score(self):
        pts = [(0, 10), (2, 35), (6, 100)]
        self.assertEqual(oc.lerp_score(-1, pts), 10)
        self.assertEqual(oc.lerp_score(9, pts), 100)
        self.assertTrue(math.isclose(oc.lerp_score(4, pts), 67.5))



class LeadingGapTest(unittest.TestCase):
    """L'anneau ouvre la fenêtre de sommeil quelques minutes avant ses premières
    données de nuit : ces epochs sans données sont de l'éveil (latence)."""

    def test_epochs_before_first_data_are_awake(self):
        n, s_ds, lead = 200, 0, 24
        motion = [(s_ds + (i + 1) * staging.EPOCH_DS, 0.1) for i in range(lead, n)]
        beats, t = [], s_ds + lead * staging.EPOCH_DS
        while t < s_ds + n * staging.EPOCH_DS:
            t += 10.0                                  # 1 battement/s (ds)
            beats.append((t, 1000))
        self.assertEqual(staging.leading_gap(n, s_ds, motion, beats), lead)
        stages, feats, _ = staging.stage_night(n, s_ds, motion, beats)
        self.assertEqual(feats["lead"], lead)
        self.assertTrue(all(x == "awake" for x in stages[:lead]))
        sm = staging.summarize(stages, feats["motion"])
        self.assertGreaterEqual(sm["latency"], lead * 30)

    def test_no_data_no_forcing(self):
        self.assertEqual(staging.leading_gap(50, 0, [], []), 0)


class LatencyNightTest(Base):
    nights = 3
    sick_last = False

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.raw = os.path.join(cls.tmp.name, "oura.db")
        cls.der = os.path.join(cls.tmp.name, "derived.db")
        cls.ring = synth.build(cls.raw, nights=cls.nights, lead_min=12)
        dn.run(cls.raw, cls.der, recompute_all=True, log=lambda *_: None)
        cls.con = sqlite3.connect(f"file:{cls.raw}?mode=ro", uri=True)
        d = sqlite3.connect(cls.der)
        d.row_factory = sqlite3.Row
        cls.rows = [dict(r) for r in d.execute("SELECT * FROM sleep_scores ORDER BY night")]
        cls.derived = d

    def test_latency_measured(self):
        for r in self.rows:
            self.assertGreaterEqual(r["latency"], 11, r["night"])   # gigue ± 1 s des paquets
            self.assertLess(r["latency"], 30, r["night"])


class OpenNightTest(Base):
    """Au réveil, tant qu'aucun signe d'éveil n'a suivi la fin de la fenêtre,
    la nuit est provisoire et aucun résumé n'est rédigé."""
    nights = 5
    sick_last = False

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.raw = os.path.join(cls.tmp.name, "oura.db")
        cls.der = os.path.join(cls.tmp.name, "derived.db")
        cls.ring = synth.build(cls.raw, nights=cls.nights)
        cls.con = sqlite3.connect(f"file:{cls.raw}?mode=ro", uri=True)
        cls.axis = oc.TimeAxis(cls.con)
        cls.raw_nights = oc.load_nights(cls.con, cls.axis)
        cls.wake = cls.raw_nights[-1]["end_unix"]
        dn.run(cls.raw, cls.der, recompute_all=True, now=cls.wake + 3600, log=lambda *_: None)
        d = sqlite3.connect(cls.der)
        d.row_factory = sqlite3.Row
        cls.rows = [dict(r) for r in d.execute("SELECT * FROM sleep_scores ORDER BY night")]
        cls.act = {r["day"]: dict(r) for r in d.execute("SELECT * FROM activity_daily")}
        cls.derived = d

    def test_closure_rule(self):
        last, prev = self.raw_nights[-1], self.raw_nights[-2]
        self.assertFalse(dn.night_closed(self.con, self.axis, last, last["end_unix"] + 3600)[0])
        ok, why = dn.night_closed(self.con, self.axis, last, last["end_unix"] + dn.CLOSE_FALLBACK_S)
        self.assertTrue(ok)
        self.assertIn("délai", why)
        # la nuit précédente : l'activité de la journée suivante prouve le réveil
        ok, why = dn.night_closed(self.con, self.axis, prev, prev["end_unix"] + 6 * 3600 - 300)
        self.assertTrue(ok)
        self.assertIn("activité", why)

    def test_only_last_night_provisional(self):
        self.assertEqual(self.rows[-1]["provisional"], 1)
        self.assertTrue(all(r["provisional"] == 0 for r in self.rows[:-1]))

    def test_no_briefing_on_open_night(self):
        calls = []
        orig = dn.llm_briefing
        dn.llm_briefing = lambda *a, **k: calls.append(a) or "texte"
        orig_choose = dn.choose_model
        dn.choose_model = lambda: dn.LLM_MODEL
        try:
            dn.maybe_briefing(self.derived, self.rows[-1], self.wake + 3600, log=lambda *_: None)
            self.assertEqual(calls, [])
            closed = dict(self.rows[-1], provisional=0)
            dn.maybe_briefing(self.derived, closed, self.wake + 3600, log=lambda *_: None)
            self.assertEqual(len(calls), 1)
        finally:
            dn.llm_briefing, dn.choose_model = orig, orig_choose

    def test_partial_day_has_no_score(self):
        today = max(self.act)
        self.assertEqual(self.act[today]["partial"], 1)
        self.assertIsNone(self.act[today]["score"])
        complete = [a for a in self.act.values() if not a["partial"]]
        self.assertTrue(complete and all(a["score"] is not None for a in complete[2:]))


class HealthyReferenceTest(Base):
    """Nuit malade au milieu de la série (signes marqués) et nuit notée
    « maladie » dans le journal : exclues des références des nuits suivantes."""
    nights = 8
    sick_last = False

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.raw = os.path.join(cls.tmp.name, "oura.db")
        cls.der = os.path.join(cls.tmp.name, "derived.db")
        cls.ring = synth.build(cls.raw, nights=cls.nights, sick=(4,))
        d = sqlite3.connect(cls.der)
        dn.ensure_schema(d)
        # tag du jour J → nuit J+1 : la nuit d'indice 6 se termine le 08/09
        d.execute("INSERT INTO tags (day, kind, note, created_unix) VALUES ('2026-09-07', 'maladie', NULL, 0)")
        d.commit()
        d.close()
        dn.run(cls.raw, cls.der, recompute_all=True, log=lambda *_: None)
        cls.con = sqlite3.connect(f"file:{cls.raw}?mode=ro", uri=True)
        d = sqlite3.connect(cls.der)
        d.row_factory = sqlite3.Row
        cls.rows = [dict(r) for r in d.execute("SELECT * FROM sleep_scores ORDER BY night")]
        cls.readiness = {r["day"]: dict(r) for r in d.execute("SELECT * FROM readiness")}
        cls.derived = d

    def test_exclusions(self):
        self.assertEqual(self.rows[6]["night"], "2026-09-08")
        self.assertEqual(self.rows[4]["ref_excluded"], "signes de tension marqués")
        self.assertEqual(self.rows[6]["ref_excluded"], "maladie (journal)")
        self.assertEqual([r["night"] for r in self.rows if r["ref_excluded"]],
                         [self.rows[4]["night"], self.rows[6]["night"]])

    def test_references_skip_sick_nights(self):
        last = self.rows[-1]
        self.assertEqual(self.readiness[last["night"]]["n_base"], 5)      # 0,1,2,3,5
        ref = json.loads(last["ref_json"])
        healthy = [r["hr_min"] for i, r in enumerate(self.rows[:-1]) if i not in (4, 6)]
        self.assertAlmostEqual(ref["hr_min"]["mean"], sum(healthy) / len(healthy), places=2)
        b = dict(self.derived.execute("SELECT metric, n FROM baselines").fetchall())
        self.assertEqual(b["hr_min"], 5)
        # la nuit qui suit la nuit malade n'est pas comparée à elle
        after = self.rows[5]
        self.assertEqual(json.loads(after["ref_json"])["hr_min"]["n"], 4)
        self.assertEqual(json.loads(self.readiness[after["night"]]["tension"])["level"], "aucun")

    def test_briefing_uses_same_references(self):
        night = self.rows[-1]["night"]
        cmp_ = json.loads(dn.briefing_context(self.derived, night))["comparaison_a_vos_references"]
        ref = json.loads(self.rows[-1]["ref_json"])
        self.assertEqual(cmp_["fc_la_plus_basse_bpm"]["nuits_de_reference"], 5)
        self.assertAlmostEqual(cmp_["fc_la_plus_basse_bpm"]["reference"], round(ref["hr_min"]["mean"], 1))



class LotBUnitTest(unittest.TestCase):
    def test_bmr_mifflin(self):
        self.assertAlmostEqual(oc.bmr_kcal(27, 185, 82, "male"), 1846.25)
        self.assertAlmostEqual(oc.bmr_kcal(27, 185, 82, "female"), 1846.25 - 166)
        self.assertIsNone(oc.bmr_kcal(None, 185, 82, "male"))

    def test_discharge_rate(self):
        t0 = 1_800_000_000
        pts = [(t0 + h * 3600, 100 - round(0.4 * h), None) for h in range(0, 49, 2)]
        cur, typ = oc.discharge_rate(pts, [])
        self.assertAlmostEqual(cur, 0.4, delta=0.03)
        self.assertAlmostEqual(typ, 0.4, delta=0.03)
        # charge récente (< 6 h de recul) : pas de pente en cours, la typique reste
        cur, typ = oc.discharge_rate(pts + [(t0 + 52 * 3600, 99, None)], [(t0 + 49 * 3600, t0 + 51 * 3600)])
        self.assertIsNone(cur)
        self.assertAlmostEqual(typ, 0.4, delta=0.03)

    def test_sleep_debt(self):
        def nights(totals):
            return [{"night": f"2026-09-{d:02d}", "total": t} for d, t in enumerate(totals, start=1)]
        self.assertEqual(dn.sleep_debt(3, nights([6.0] * 4)), (None, 4))       # < 5 nuits
        debt, k = dn.sleep_debt(13, nights([dn.SLEEP_NEED_H] * 14))
        self.assertEqual((debt, k), (0.0, 14))
        debt, _ = dn.sleep_debt(13, nights([dn.SLEEP_NEED_H - 1] * 14))
        self.assertAlmostEqual(debt, 14.0, places=6)                         # poids moyens = 1
        # les nuits récentes pèsent davantage ; un surplus rembourse
        recent, _ = dn.sleep_debt(13, nights([dn.SLEEP_NEED_H] * 13 + [dn.SLEEP_NEED_H - 2]))
        old, _ = dn.sleep_debt(13, nights([dn.SLEEP_NEED_H - 2] + [dn.SLEEP_NEED_H] * 13))
        self.assertGreater(recent, old)
        paid, _ = dn.sleep_debt(13, nights([dn.SLEEP_NEED_H - 1] * 13 + [dn.SLEEP_NEED_H + 3]))
        self.assertLess(paid, 14.0 - 1)


class LotBDayTest(Base):
    """Pas (octet d'en-tête du tag 80), dépense totale, repos, dette de sommeil."""
    nights = 6
    sick_last = False

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.raw = os.path.join(cls.tmp.name, "oura.db")
        cls.der = os.path.join(cls.tmp.name, "derived.db")
        cls.ring = synth.build(cls.raw, nights=cls.nights, rests=((2, 14, 25),))
        d = sqlite3.connect(cls.der)
        dn.ensure_schema(d)
        d.execute("INSERT INTO user_profile VALUES (1, 27, 185, 82, 'male', 0)")
        d.commit()
        d.close()
        dn.run(cls.raw, cls.der, recompute_all=True, log=lambda *_: None)
        cls.con = sqlite3.connect(f"file:{cls.raw}?mode=ro", uri=True)
        d = sqlite3.connect(cls.der)
        d.row_factory = sqlite3.Row
        cls.act = {r["day"]: dict(r) for r in d.execute("SELECT * FROM activity_daily")}
        cls.rests = [dict(r) for r in d.execute("SELECT * FROM rests")]
        cls.readiness = [dict(r) for r in d.execute("SELECT * FROM readiness ORDER BY day")]
        cls.derived = d

    def test_steps_are_event_header_counts(self):
        ax = oc.TimeAxis(self.con)
        day = "2026-09-03"
        t0, t1 = oc.activity_day_bounds(day)                     # 04:00 → 04:00
        self.assertEqual(t1 - t0, 86400)
        self.assertEqual(t0 - datetime_ts(day), 4 * 3600)
        n = 0
        for rt, cap, dj in self.con.execute("SELECT ring_timestamp, captured_unix, decoded_json FROM events WHERE tag=80"):
            end = ax(rt, cap)
            k = len(json.loads(dj)["met"])
            if t0 <= end - k * 60 and end <= t1:
                n += json.loads(dj)["state"]
        self.assertGreater(n, 0)
        self.assertAlmostEqual(self.act[day]["steps"], n, delta=1)

    def test_total_energy(self):
        a = self.act["2026-09-03"]
        self.assertEqual(a["bmr_kcal"], 1846)
        self.assertEqual(a["total_kcal"], a["bmr_kcal"] + a["active_kcal"])

    def test_rest_period_recorded(self):
        self.assertEqual(len(self.rests), 1)
        self.assertEqual(self.rests[0]["kind"], "repos")
        self.assertAlmostEqual(self.rests[0]["minutes"], 25, delta=0.5)
        self.assertEqual(self.act["2026-09-03"]["rest_min"], 25)

    def test_sleep_debt_stored(self):
        self.assertIsNone(self.readiness[3]["sleep_debt_h"])
        self.assertEqual(self.readiness[4]["sleep_debt_n"], 5)
        rows = dn.nights_table(self.derived)
        self.assertEqual(self.readiness[4]["sleep_debt_h"], dn.sleep_debt(4, rows)[0])
        self.assertGreater(self.readiness[4]["sleep_debt_h"], 0)            # sommeil < 7 h 30 au lit
        self.assertEqual(self.readiness[4]["sleep_need_h"], dn.SLEEP_NEED_H)



class ActivityGridTest(unittest.TestCase):
    """Grille minute par minute : port aux doigts froids, retraits, périodes assises."""

    def test_activity_day(self):
        from datetime import datetime
        self.assertEqual(oc.activity_day(datetime(2026, 9, 3, 3, 59).timestamp()), "2026-09-02")
        self.assertEqual(oc.activity_day(datetime(2026, 9, 3, 4, 0).timestamp()), "2026-09-03")

    def test_sedentary_bouts_tolerate_brief_movement(self):
        def grid(mets):
            return {i * 60: {"state": "éveil", "met": v, "motion": 0, "steps": 0} for i, v in enumerate(mets)}
        self.assertEqual(dn.sedentary_bouts(grid([1.2] * 59)), 0)
        self.assertEqual(dn.sedentary_bouts(grid([1.2] * 60)), 1)
        self.assertEqual(dn.sedentary_bouts(grid([1.2] * 40 + [1.8] * 2 + [1.2] * 30)), 1)   # s'agiter
        self.assertEqual(dn.sedentary_bouts(grid([1.2] * 40 + [2.5] * 3 + [1.2] * 30)), 0)   # se lever
        g = grid([1.2] * 130)
        for i in range(60, 63):
            g[i * 60]["state"] = "charge"
        self.assertEqual(dn.sedentary_bouts(g), 2)


class ColdFingersTest(Base):
    """Peau < 30 °C mais mouvement et activité : la minute est portée."""
    nights = 4
    sick_last = False

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.raw = os.path.join(cls.tmp.name, "oura.db")
        cls.der = os.path.join(cls.tmp.name, "derived.db")
        cls.ring = synth.build(cls.raw, nights=cls.nights)
        con = sqlite3.connect(cls.raw)
        con.execute("UPDATE events SET decoded_json=? WHERE tag=70", (json.dumps({"temps_c": [26.0, 26.0, 28.0]}),))
        con.commit()
        con.close()
        cls.con = sqlite3.connect(f"file:{cls.raw}?mode=ro", uri=True)
        cls.derived = sqlite3.connect(":memory:")

    def test_active_minutes_count_as_worn(self):
        ax = oc.TimeAxis(self.con)
        t0, t1 = oc.activity_day_bounds("2026-09-03")
        g = oc.activity_grid(self.con, ax, t0, t1)
        active = [x for x in g.values() if x["met"] is not None and x["met"] >= oc.MET_LIGHT]
        self.assertTrue(active)
        self.assertTrue(all(x["state"] == "éveil" for x in active))
        calm = [x for x in g.values() if x["met"] is not None and x["met"] < oc.MET_LIGHT]
        self.assertTrue(all(x["state"] == "non porté" for x in calm))   # ni chaleur, ni mouvement
        # retrait signalé : prioritaire sur l'activité
        m0 = min(m for m, x in g.items() if x in active)
        g2 = oc.activity_grid(self.con, ax, t0, t1, removals=[(m0, m0 + 60)])
        self.assertEqual(g2[m0]["state"], "retiré")


def datetime_ts(day):
    from datetime import datetime
    return datetime.strptime(day, "%Y-%m-%d").timestamp()



class LotA2Test(Base):
    """Indice de récupération par stabilisation, température médiane, plafonds,
    résumés inachevés."""
    nights = 6
    sick_last = False

    def test_recovery_from_stabilisation(self):
        for r in self.rows:
            self.assertIsNotNone(r["hr_stable_at"])
            self.assertLessEqual(r["hr_stable_at"], r["hr_lowest_at"])
            # plus de sommeil après la stabilisation qu'après le minimum strict
            self.assertGreaterEqual(r["recovery_index"], 4)

    def test_temperature_is_median_of_sleep_samples(self):
        for r in self.rows:
            self.assertAlmostEqual(r["temp_mean"], 35.2, delta=0.1)
            self.assertIsNotNone(r["temp_ecore"])

    def test_caps_hold(self):
        for r in self.rows:
            sleep = r["deep"] + r["rem"] + r["light"]
            self.assertLessEqual(r["deep"] / sleep, staging.CAPS["deep"] + 0.005)
            self.assertLessEqual(r["rem"] / sleep, staging.CAPS["rem"] + 0.005)

    def test_hrv_balance_calibrating(self):
        c = json.loads(self.readiness[self.rows[-1]["night"]]["contributors"])
        self.assertEqual(c["hrv_balance"]["status"], "calibrage")
        self.assertIsNone(c["hrv_balance"]["score"])

    def test_truncated_briefing(self):
        self.assertTrue(dn.truncated("Vous avez dormi 7 h 13 avec une efficacité de 89"))
        self.assertFalse(dn.truncated("Phrase complète. " * 15))


if __name__ == "__main__":
    unittest.main()
