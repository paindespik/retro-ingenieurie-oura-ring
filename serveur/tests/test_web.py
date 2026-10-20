"""Tests de l'API (FastAPI TestClient) sur une base synthétique.

Ignorés si fastapi/httpx ne sont pas installés.
"""
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

try:
    from fastapi.testclient import TestClient
except ImportError:                      # pragma: no cover
    TestClient = None


@unittest.skipIf(TestClient is None, "fastapi non installé")
class WebTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import derive_night as dn
        import synth
        cls.tmp = tempfile.TemporaryDirectory()
        raw = os.path.join(cls.tmp.name, "oura.db")
        der = os.path.join(cls.tmp.name, "derived.db")
        synth.build(raw, nights=4, sick_last=True)
        dn.run(raw, der, recompute_all=True, log=lambda *_: None)
        os.environ.update(OURA_RAW_DB=raw, OURA_DERIVED_DB=der, OURA_PHONE_TOKEN="jeton-de-test")
        import importlib
        import oura_web
        cls.web = importlib.reload(oura_web)
        cls.c = TestClient(cls.web.app)
        import sqlite3
        con = sqlite3.connect(der)
        cls.night = dn.nights_table(con)[-1]["night"]
        con.close()

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_pages_and_routes(self):
        self.assertEqual(self.c.get("/").status_code, 200)
        self.assertEqual(self.c.get("/static/app.js").status_code, 200)
        for u in ("/api/overview", "/api/nights", f"/api/night?date={self.night}", "/api/readiness",
                  f"/api/readiness?date={self.night}", "/api/trends?days=30",
                  f"/api/activity?date={self.night}", "/api/telemetry?hours=48", "/api/events?limit=5",
                  "/api/health", "/api/tags"):
            r = self.c.get(u)
            self.assertEqual(r.status_code, 200, (u, r.text[:200]))

    def test_night_payload(self):
        d = self.c.get(f"/api/night?date={self.night}").json()
        self.assertTrue(d["staging"]["stages"])
        self.assertTrue(set(d["staging"]["stages"]) <= set("WLDR"))
        self.assertTrue(d["series"])
        self.assertEqual(d["readiness"]["tension"]["level"], "marqués")

    def test_lot_b_routes(self):
        b = self.c.get("/api/battery?days=14").json()
        self.assertEqual(b["points"], [])
        self.assertIsNone(b["remaining_h"])
        qd = self.c.get("/api/quality?days=30").json()
        self.assertEqual(len(qd["nights"]), 4)
        self.assertIn("beats_kept_pct", qd["nights"][0])
        t = self.c.get("/api/trends?days=30").json()
        self.assertIn("sleep_debt_h", t["nights"][-1])
        self.assertIn("steps", t["activity"][0])
        self.assertIn("tags", t)
        a = self.c.get(f"/api/activity?date={self.night}").json()
        self.assertIn("steps_hourly", a)
        self.assertIn("rest_list", a)
        import json
        ctx = json.loads(self.web.chat_context())
        self.assertIn("references_personnelles", ctx)
        self.assertRegex(ctx["nuits"][-1]["sommeil_total"], r"^\d+ h \d\d$")

    def test_night_uses_its_own_references(self):
        d = self.c.get(f"/api/night?date={self.night}").json()
        self.assertNotIn("ref", d["night"])
        self.assertEqual(d["baselines"]["hr_min"]["n"], 3)          # les 3 nuits saines précédentes
        first = self.c.get("/api/nights").json()["nights"][0]["night"]
        self.assertEqual(self.c.get(f"/api/night?date={first}").json()["baselines"], {})
        o = self.c.get("/api/overview").json()
        self.assertIn("provisional", o["night"])

    def test_overview_state_is_text(self):
        o = self.c.get("/api/overview").json()
        st = o["ring"]["state"]
        self.assertTrue(st is None or isinstance(st["label"], str))

    def test_tags_roundtrip(self):
        r = self.c.post("/api/tags", json={"day": self.night, "kind": "maladie", "note": "test"})
        self.assertEqual(r.status_code, 200)
        tid = r.json()["id"]
        self.assertTrue(any(t["id"] == tid for t in self.c.get("/api/tags").json()["tags"]))
        self.assertEqual(self.c.delete(f"/api/tags/{tid}").status_code, 200)
        self.assertEqual(self.c.post("/api/tags", json={"day": "x", "kind": "maladie"}).status_code, 422)
        self.assertEqual(self.c.post("/api/tags", json={"day": self.night, "kind": "?"}).status_code, 422)

    def test_cellier_not_configured(self):
        self.assertFalse(self.web._cellier_configured())
        self.assertEqual(self.c.get("/api/cellier").status_code, 503)

    def test_cellier_password_flow(self):
        import hashlib
        salt = bytes(16)
        h = hashlib.scrypt(b"test-cellier", salt=salt, n=16384, r=8, p=1, dklen=32)
        saved = self.web.CELLIER_HASH
        self.web.CELLIER_HASH = f"scrypt$16384$8$1${salt.hex()}${h.hex()}"
        try:
            self.assertEqual(self.c.post("/api/cellier/auth", json={"password": "mauvais"}).status_code, 401)
            r = self.c.post("/api/cellier/auth", json={"password": "test-cellier"})
            self.assertEqual(r.status_code, 200)
            # table absente (base synthétique) → liste vide, pas d'erreur
            r = self.c.get("/api/cellier")
            self.assertEqual(r.status_code, 200)
            self.assertEqual(r.json()["episodes"], [])
        finally:
            self.web.CELLIER_HASH = saved

    def test_cellier_requires_password(self):
        # pas de cookie → 401 même si le hash est configuré
        import hashlib
        salt = bytes(16)
        h = hashlib.scrypt(b"test-cellier", salt=salt, n=16384, r=8, p=1, dklen=32)
        saved = self.web.CELLIER_HASH
        self.web.CELLIER_HASH = f"scrypt$16384$8$1${salt.hex()}${h.hex()}"
        try:
            c2 = TestClient(self.web.app)   # jar de cookies neuf, sans session
            self.assertEqual(c2.get("/api/cellier").status_code, 401)
        finally:
            self.web.CELLIER_HASH = saved

    def test_ingest_auth(self):
        body = {"serial": "0000000000000000", "cursor": 1, "events": [
            {"tag": 66, "ring_timestamp": 1, "body_hex": "00ff", "decoded_json": {"unix_time": 1}}]}
        self.assertEqual(self.c.post("/ingest/events", json=body).status_code, 401)
        self.assertEqual(self.c.post("/ingest/events", json=body, headers={"X-Oura-Token": "faux"}).status_code, 401)
        r = self.c.post("/ingest/events", json=body, headers={"X-Oura-Token": "jeton-de-test"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["inserted"], 1)
        r = self.c.post("/ingest/events", json=body, headers={"X-Oura-Token": "jeton-de-test"})
        self.assertEqual(r.json()["inserted"], 0, "déduplication")

    def test_no_spo2_percent_anywhere(self):
        t = self.c.get("/api/telemetry?hours=168").json()
        self.assertNotIn("spo2_percent", str(t))
        js = self.c.get("/static/app.js").text
        self.assertNotIn("110 - 25", js)
        self.assertNotIn("110 − 25", js)


if __name__ == "__main__":
    unittest.main()
