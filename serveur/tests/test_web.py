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


    # --------------------------------------------------- outils de l'analyse approfondie

    def _tool(self, name, **args):
        import json
        out = json.loads(self.web.run_tool(name, args))
        return out

    def test_chat_tools(self):
        n = self.night
        c = self._tool("courbes_nuit", date=n)
        self.assertNotIn("erreur", c)
        self.assertTrue(c["points_5_min"])
        self.assertEqual(len(c["points_5_min"][0]), len(c["colonnes"]))
        self.assertTrue(c["hypnogramme_estime"])
        self.assertTrue(all(x[2] in ("éveil", "léger", "profond", "REM") for x in c["hypnogramme_estime"]))
        self.assertEqual(len(self._tool("nuits", debut="2000-01-01", fin="2100-01-01")["nuits"]), 4)
        self.assertNotIn("erreur", self._tool("recuperation", date=n))
        a = self._tool("activite_jour", date=n)
        self.assertNotIn("erreur", a)
        self.assertIn("par_quart_d_heure", a)
        self.assertIn("journal", self._tool("journal", debut="2000-01-01", fin="2100-01-01"))
        start = c["coucher"]          # « JJ/MM HH:MM »
        self.assertTrue(start)
        z = self._tool("zoom", debut=f"{n} 00:00", fin=f"{n} 03:00")
        self.assertNotIn("erreur", z)
        self.assertIn("par_minute", z)

    def test_chat_stats_and_clock(self):
        import json
        import sqlite3
        ctx = json.loads(self.web.chat_context())
        self.assertRegex(ctx["maintenant"], r"^\w+ \d\d/\d\d/\d{4} \d\d:\d\d$")
        con = sqlite3.connect(os.environ["OURA_DERIVED_DB"])
        rows = con.execute("SELECT night, hr_min FROM sleep_scores WHERE provisional=0 AND hr_min IS NOT NULL").fetchall()
        con.close()
        st = ctx["statistiques_nuits"]["fc_la_plus_basse_bpm"]
        lo = min(rows, key=lambda r: r[1])
        self.assertEqual((st["min"], st["date_min"]), (round(lo[1], 1), lo[0]))
        self.assertAlmostEqual(st["moyenne"], round(sum(r[1] for r in rows) / len(rows), 1))
        self.assertRegex(ctx["statistiques_nuits"]["sommeil_total"]["moyenne"], r"^\d+ h \d\d$")
        t = self._tool("statistiques", debut="2000-01-01", fin="2100-01-01")
        self.assertNotIn("erreur", t)
        self.assertEqual(t["sommeil"]["fc_la_plus_basse_bpm"]["date_min"], lo[0])

    def test_clock_stats_wrap_midnight(self):
        from datetime import datetime
        u = lambda s: datetime.strptime(s, "%Y-%m-%d %H:%M").timestamp()  # noqa: E731
        rows = [{"night": "2026-10-01", "t": u("2026-09-30 23:30")},
                {"night": "2026-10-02", "t": u("2026-10-02 01:30")}]
        st = self.web._clock_stat(rows, lambda r: r["t"])
        self.assertEqual(st["moyenne"], "00:30")          # pas 12:30
        self.assertEqual((st["plus_tot"], st["plus_tard"]), ("23:30", "01:30"))
        self.assertEqual(st["amplitude"], "2 h 00")
        self.assertEqual(st["ecart_type_min"], 85)

    def test_chat_tools_bad_args(self):
        self.assertIn("erreur", self._tool("courbes_nuit", date="hier"))
        self.assertIn("erreur", self._tool("courbes_nuit", date="1999-01-01"))
        self.assertIn("erreur", self._tool("zoom", debut="2026-01-01 00:00", fin="2026-01-01 09:00"))
        self.assertIn("erreur", self._tool("nimporte"))
        self.assertIn("erreur", self._tool("nuits", debut="2026-01-01"))       # argument manquant

    def test_deep_chat_tool_loop(self):
        """Boucle modèle ↔ outils avec un faux llama-swap : appel d'outil, puis réponse."""
        import asyncio
        import json
        web, night = self.web, self.night
        sent = []

        class Resp:
            def __init__(self, msg):
                self.msg = msg

            def raise_for_status(self):
                pass

            def json(self):
                return {"choices": [{"message": self.msg}]}

        class Client:
            def __init__(self, **_):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_):
                return False

            async def post(self, _url, json=None):
                sent.append(json)
                if len(sent) == 1:
                    return Resp({"content": "", "tool_calls": [{"id": "a", "type": "function", "function": {
                        "name": "courbes_nuit", "arguments": '{"date": "%s"}' % night}}]})
                return Resp({"content": "Votre FC la plus basse était à 03:10."})

        orig = web.httpx.AsyncClient
        web.httpx.AsyncClient = Client
        try:
            job = {"tools": []}
            asyncio.run(web.deep_chat_job(job, "À quelle heure ma FC était-elle la plus basse ?"))
        finally:
            web.httpx.AsyncClient = orig
        self.assertEqual(job["state"], "done", job)
        self.assertEqual(job["tools"], [f"courbes_nuit({night})"])
        self.assertIn("tools", sent[0])
        tool_msg = sent[1]["messages"][-1]
        self.assertEqual(tool_msg["role"], "tool")
        self.assertIn("points_5_min", json.loads(tool_msg["content"]))


if __name__ == "__main__":
    unittest.main()
