#!/usr/bin/env python3
"""Générateur de base synthétique pour tester oura-web sans anneau.

Crée /srv/oura/oura.db (schéma open_oura) + /srv/oura/derived.db (tables dérivées)
avec 30 nuits plausibles.  Usage : make_synth.py [nb_nuits] [prefixe]
"""
import json
import math
import random
import sqlite3
import sys
import time
from datetime import date, timedelta

random.seed(42)
N = int(sys.argv[1]) if len(sys.argv) > 1 else 30
PREFIX = sys.argv[2] if len(sys.argv) > 2 else "/srv/oura"

RAW_SCHEMA = """
CREATE TABLE IF NOT EXISTS device (
    serial TEXT PRIMARY KEY, hardware_id TEXT, firmware TEXT,
    api_version TEXT, mac TEXT, updated_unix INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS sync_state (
    serial TEXT PRIMARY KEY, next_cursor INTEGER NOT NULL, last_sync_unix INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    serial TEXT NOT NULL, tag INTEGER NOT NULL, name TEXT NOT NULL,
    ring_timestamp INTEGER NOT NULL, body BLOB NOT NULL,
    decoded_json TEXT, captured_unix INTEGER NOT NULL,
    UNIQUE(serial, tag, ring_timestamp, body));
CREATE TABLE IF NOT EXISTS readings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    serial TEXT NOT NULL, kind TEXT NOT NULL, value REAL NOT NULL,
    unit TEXT, captured_unix INTEGER NOT NULL);
"""

DERIVED_SCHEMA = """
CREATE TABLE IF NOT EXISTS sleep_scores (
    night TEXT PRIMARY KEY, score REAL, total REAL, deep REAL, rem REAL,
    efficiency REAL, latency REAL, timing REAL, restfulness REAL,
    hr_min REAL, hr_mean REAL, hrv_rmssd REAL, temp_dev REAL,
    movement REAL, spo2 REAL, bedtime TEXT, wake_time TEXT, staging_source TEXT);
CREATE TABLE IF NOT EXISTS night_staging (
    night TEXT, epoch INTEGER, stage TEXT, PRIMARY KEY (night, epoch));
CREATE TABLE IF NOT EXISTS llm_briefings (
    night TEXT, model TEXT, text TEXT, ts INTEGER);
CREATE TABLE IF NOT EXISTS baselines (
    metric TEXT PRIMARY KEY, mean REAL, sd REAL, updated_unix INTEGER);
"""

SERIAL = "RING_SERIAL"


def night_rows():
    """30 nuits de métriques plausibles avec tendance douce."""
    rows = []
    today = date.today()
    for i in range(N - 1, -1, -1):
        night = today - timedelta(days=i + 1)  # la nuit dernière = hier
        phase = i / 7.0
        score = 72 + 8 * math.sin(phase) + random.gauss(0, 4)
        hr_mean = 52 + random.gauss(0, 2)
        hr_min = hr_mean - 7 + random.gauss(0, 1)
        rmssd = 55 + 6 * math.sin(phase / 2) + random.gauss(0, 5)
        temp_dev = random.gauss(0, 0.25)
        total = 7.2 + random.gauss(0, 0.8)
        deep = 1.2 + random.gauss(0, 0.2)
        rem = 1.6 + random.gauss(0, 0.25)
        latency = 14 + abs(random.gauss(0, 8))
        bt_h, bt_m = 23, int(random.gauss(15, 20))
        wt_h = (bt_h + int(total)) % 24
        rows.append((
            night.isoformat(), round(min(100, max(20, score)), 1),
            round(total, 2), round(deep, 2), round(rem, 2),
            round(95 - abs(random.gauss(2, 2)), 1),
            round(latency, 1),
            round(80 + random.gauss(0, 8), 1),
            round(75 + random.gauss(0, 10), 1),
            round(hr_min, 1), round(hr_mean, 1), round(rmssd, 1),
            round(temp_dev, 2), round(22 + random.gauss(0, 4), 1),
            round(96 + random.gauss(0, 1), 1),
            f"{bt_h:02d}:{max(0, min(59, bt_m)):02d}",
            f"{wt_h:02d}:{int(random.gauss(20, 15)):02d}",
            "heuristic" if i % 3 else "ring",
        ))
    return rows


def staging(night_iso):
    """Hypnogramme synthétique : ~8 h en epochs de 30 s."""
    stages = []
    cur = "light"
    for ep in range(int(8 * 120)):
        r = random.random()
        if cur in ("deep", "rem") and r < 0.75:
            nxt = cur
        else:
            nxt = random.choices(["deep", "rem", "light", "awake"], [0.18, 0.22, 0.52, 0.08])[0]
        stages.append((night_iso, ep, nxt))
        cur = nxt
    return stages


def main():
    now = int(time.time())
    raw = sqlite3.connect(f"{PREFIX}/oura.db")
    raw.executescript(RAW_SCHEMA)
    raw.execute("DELETE FROM device"); raw.execute("DELETE FROM sync_state")
    raw.execute("DELETE FROM events"); raw.execute("DELETE FROM readings")
    raw.execute("INSERT INTO device VALUES (?,?,?,?,?,?)",
                (SERIAL, "ORE_05", "2.1.3", "2.1.0", "56:EF:80:F5:93:CE", now))
    raw.execute("INSERT INTO sync_state VALUES (?,?,?)", (SERIAL, 123456, now))
    # quelques événements typiques
    tags = [(0x41, "ring_start"), (0x09, "battery"), (0x03, "hr_5min"),
            (0x0a, "temperature"), (0x0d, "hrv_rmssd_5min")]
    ts = now - 3600
    for k in range(500):
        tag, name = random.choice(tags)
        raw.execute(
            "INSERT OR IGNORE INTO events (serial,tag,name,ring_timestamp,body,decoded_json,captured_unix) "
            "VALUES (?,?,?,?,?,?,?)",
            (SERIAL, tag, name, ts + k * 60, b"\x01\x02", json.dumps({"v": random.random()}), now))
    raw.execute("INSERT INTO readings (serial,kind,value,unit,captured_unix) VALUES (?,?,?,?,?)",
                (SERIAL, "battery", 87.0, "%", now))
    raw.commit()

    der = sqlite3.connect(f"{PREFIX}/derived.db")
    der.executescript(DERIVED_SCHEMA)
    for t in ("sleep_scores", "night_staging", "llm_briefings", "baselines"):
        der.execute(f"DELETE FROM {t}")
    rows = night_rows()
    der.executemany("INSERT INTO sleep_scores VALUES (" + ",".join("?" * 18) + ")", rows)
    for night, *_ in rows:
        der.executemany("INSERT OR IGNORE INTO night_staging VALUES (?,?,?)", staging(night))
    der.execute("INSERT INTO llm_briefings VALUES (?,?,?,?)",
                (rows[-1][0], "qwen3.5-4b",
                 "Nuit correcte : score 74, HR moyen 52 bpm, RMSSD 58 ms (dans ta baseline). "
                 "La latence d'endormissement (22 min) reste au-dessus de ta moyenne — pense à "
                 "couper les écrans 30 min avant. Aucune déviance de température.", now))
    der.execute("INSERT INTO baselines VALUES (?,?,?,?)", ("hrv_rmssd", 55.0, 6.0, now))
    der.execute("INSERT INTO baselines VALUES (?,?,?,?)", ("hr_mean", 52.0, 2.0, now))
    der.commit()
    print(f"OK: {N} nuits synthétiques dans {PREFIX}/oura.db + {PREFIX}/derived.db")


if __name__ == "__main__":
    main()
