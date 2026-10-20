"""Tests de la détection Cellier (bibliothèque standard uniquement).

Base brute synthétique :
  - jour A : baseline 80, deux pics (148/146) à 2 min d'intervalle, MET 1,2,
    peau 34 → 35,5 (montée lente) → épisode « fort » ;
  - jour B : un seul pic + montée cutanée 0,7 °C → épisode « possible » ;
  - jour C : un pic pendant une chute cutanée progressive de 5 °C (douche)
    → exclu.
"""
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

import derive_cellier as dc  # noqa: E402
import oura_core as oc  # noqa: E402

DRIFT = 1.00013


def build_raw(path, t0):
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE events (id INTEGER PRIMARY KEY AUTOINCREMENT,"
                " serial TEXT NOT NULL, tag INTEGER NOT NULL, name TEXT NOT NULL,"
                " ring_timestamp INTEGER NOT NULL, body BLOB NOT NULL,"
                " decoded_json TEXT, captured_unix INTEGER NOT NULL)")
    n = [0]

    def rt(unix):
        return int(round((unix - t0) / DRIFT * 10))

    def unix(r):
        return t0 + r / 10 * DRIFT

    def ev(tag, r, dj, cap=None):
        n[0] += 1
        con.execute("INSERT INTO events (serial, tag, name, ring_timestamp, body,"
                    " decoded_json, captured_unix) VALUES (?,?,?,?,?,?,?)",
                    ("S", tag, "x", int(r), n[0].to_bytes(4, "little"),
                     json.dumps(dj), int(cap or unix(r) + 30)))

    days = 4
    t_end = t0 + days * 86400
    t = t0
    while t < t_end:
        ev(66, rt(t), {"unix_time": int(t)})   # ancres time_sync, sans bruit
        t += 900

    def beats(unix_, hr, ibi=None):
        """Fenêtre tag 128 d'un battement : hr_bpm + ibi_ms cohérents."""
        ibi = ibi if ibi is not None else [int(60000 / hr)]
        ev(128, rt(unix_), {"hr_bpm": [hr], "ibi_ms": ibi, "quality": [1]})

    def episode(t_s, peaks, skin0, skin1, dur=3600, met=1.2, pre_drop=None):
        """Épisode à t_s : battements de baseline 80, pics aux offsets donnés
        [(offset_s, bpm)], peau linéaire skin0→skin1 sur dur — ou, si pre_drop
        est donnée, chute skin0→skin1 sur [0, pre_drop] puis plate (douche),
        MET constant."""
        for off in range(0, dur, 30):
            beats(t_s + off, 80)
        for off, hr in peaks:
            beats(t_s + off, hr)
        t = t_s
        while t < t_s + dur:
            if pre_drop is not None and t - t_s < pre_drop:
                v = skin0 + (skin1 - skin0) * (t - t_s) / pre_drop
            elif pre_drop is not None:
                v = skin1
            else:
                v = skin0 + (skin1 - skin0) * (t - t_s) / dur
            ev(70, rt(t), {"temps_c": [round(v, 2), 35.0, 37.0]})
            t += 120
        t = t_s
        while t < t_s + dur:
            ev(80, rt(t), {"met": [met] * 6, "state": 0})
            t += 300

    # baseline du matin (avant chaque épisode) : battements + peau jusqu'à 8 h
    for d in range(days):
        day = t0 + d * 86400
        for off in range(0, 8 * 3600, 60):
            beats(day + off, 80)
        for off in range(0, 8 * 3600, 120):
            ev(70, rt(day + off), {"temps_c": [34.0, 35.0, 37.0]})

    # jour A : 2 pics, peau 34 → 35,5 sur 1 h → « fort »
    episode(t0 + 8 * 3600, [(300, 148), (420, 146)], 34.0, 35.5, dur=3600)
    # jour B : 1 pic, montée rapide 34 → 34,8 → « possible »
    episode(t0 + 2 * 86400 + 8 * 3600, [(900, 148)], 34.0, 34.8, dur=3600,
            pre_drop=900)
    # jour C : douche (chute 35 → 31 sur les 15 min précédant le pic) → exclu
    episode(t0 + 3 * 86400 + 8 * 3600, [(900, 148)], 35.0, 31.0, dur=3600,
            pre_drop=900)

    con.commit()
    return con


class CellierTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.raw = os.path.join(cls.tmp.name, "oura.db")
        cls.der = os.path.join(cls.tmp.name, "derived.db")
        cls.t0 = int(datetime(2026, 10, 1).timestamp())
        cls.con = build_raw(cls.raw, cls.t0)
        cls.axis = oc.TimeAxis(cls.con)
        cls.eps = dc.detect_episodes(cls.con, cls.axis, cls.t0, cls.t0 + 4 * 86400)
        cls.der_con = sqlite3.connect(cls.der)
        cls.der_con.row_factory = sqlite3.Row
        dc.run(cls.raw, cls.der, log=lambda *_: None)

    @classmethod
    def tearDownClass(cls):
        cls.con.close()
        cls.der_con.close()
        cls.tmp.cleanup()

    def test_deduction_des_episodes(self):
        days = sorted(e["day"] for e in self.eps)
        self.assertEqual(len(days), 2, f"épisodes inattendus: {self.eps}")
        a = [e for e in self.eps if e["day"] == "2026-10-01"]
        b = [e for e in self.eps if e["day"] == "2026-10-03"]
        self.assertEqual(len(a), 1)
        self.assertEqual(len(b), 1)
        self.assertEqual(a[0]["confidence"], "fort")
        self.assertGreaterEqual(a[0]["peak_min"], 2)
        self.assertLess(a[0]["met_mean"], dc.MET_MAX)
        self.assertEqual(b[0]["confidence"], "possible")
        self.assertEqual(b[0]["peak_min"], 1)

    def test_douche_exclue(self):
        self.assertFalse([e for e in self.eps if e["day"] == "2026-10-04"])

    def test_table_derivee(self):
        rows = [dict(r) for r in self.der_con.execute(
            "SELECT * FROM cellier_episodes ORDER BY start_unix")]
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["confidence"], "fort")
        self.assertEqual(rows[1]["confidence"], "possible")


if __name__ == "__main__":
    unittest.main()
