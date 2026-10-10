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
                self.assertTrue(0 <= c["score"] <= 100, (k, c))

    def test_briefing_number_guard(self):
        night = self.rows[-1]["night"]
        ctx = dn.briefing_context(self.derived, night)
        rec = json.loads(ctx)["recuperation_du_jour"]["score_recuperation"]
        sleep = json.loads(ctx)["nuit_a_resumer"]["score_sommeil"]
        good = f"Score de sommeil {sleep:.0f}, récupération {rec:.0f}. En cas d'urgence : 15 ou 112."
        self.assertEqual(dn.unverified_numbers(good, ctx), [])
        self.assertIn("987", dn.unverified_numbers("récupération 987", ctx))

    def test_model_choice_never_evicts(self):
        cases = {("qwen3.8-27b", "qwen3.5-4b"): "qwen3.8-27b",      # résident → 27B
                 ("qwen3.5-4b",): "qwen3.8-27b",                    # 3090 libre → 27B (défaut)
                 ("gemma4-31b", "qwen3.5-4b"): "qwen3.5-4b",        # 3090 occupée → 4B
                 None: "qwen3.5-4b"}                                # llama-swap illisible → 4B
        orig = dn.running_models
        try:
            for run, want in cases.items():
                dn.running_models = lambda r=run: list(r) if r else r
                self.assertEqual(dn.choose_model(), want, run)
        finally:
            dn.running_models = orig

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


if __name__ == "__main__":
    unittest.main()
