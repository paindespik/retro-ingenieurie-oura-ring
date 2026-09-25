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

    def test_lerp_score(self):
        pts = [(0, 10), (2, 35), (6, 100)]
        self.assertEqual(oc.lerp_score(-1, pts), 10)
        self.assertEqual(oc.lerp_score(9, pts), 100)
        self.assertTrue(math.isclose(oc.lerp_score(4, pts), 67.5))


if __name__ == "__main__":
    unittest.main()
