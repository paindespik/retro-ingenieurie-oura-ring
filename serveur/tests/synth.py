"""Base brute synthétique (schéma open_oura) pour les tests : nuits plausibles,
horloge d'anneau à ~130 ppm avec ancres time_sync bruitées, paquets de
battements contigus mais horodatés de façon irrégulière, bins hrv_event et
activity_information alignés sur la FIN de l'événement — comme l'anneau réel.

Aucune donnée personnelle : tout est généré (graine fixe).
"""
import json
import math
import random
import sqlite3
from datetime import datetime, timedelta

SCHEMA = """
CREATE TABLE device (serial TEXT PRIMARY KEY, hardware_id TEXT, firmware TEXT,
    api_version TEXT, mac TEXT, updated_unix INTEGER NOT NULL);
CREATE TABLE sync_state (serial TEXT PRIMARY KEY, next_cursor INTEGER NOT NULL,
    last_sync_unix INTEGER NOT NULL);
CREATE TABLE events (id INTEGER PRIMARY KEY AUTOINCREMENT, serial TEXT NOT NULL,
    tag INTEGER NOT NULL, name TEXT NOT NULL, ring_timestamp INTEGER NOT NULL,
    body BLOB NOT NULL, decoded_json TEXT, captured_unix INTEGER NOT NULL,
    UNIQUE(serial, tag, ring_timestamp, body));
CREATE TABLE readings (id INTEGER PRIMARY KEY AUTOINCREMENT, serial TEXT NOT NULL,
    kind TEXT NOT NULL, value REAL NOT NULL, unit TEXT, captured_unix INTEGER NOT NULL);
"""
SERIAL = "0000000000000000"
DRIFT = 1.00013          # ratio unix / (rt/10) de l'horloge simulée


class Ring:
    def __init__(self, path, t0_unix, seed=7):
        self.rnd = random.Random(seed)
        self.con = sqlite3.connect(path)
        self.con.executescript(SCHEMA)
        self.con.execute("INSERT INTO device VALUES (?,?,?,?,?,?)",
                         (SERIAL, None, "2.1.25", "2.1.0", "00:00:00:00:00:00", int(t0_unix)))
        self.t0 = t0_unix          # unix du rt 0
        self.n = 0

    def rt(self, unix):
        return int(round((unix - self.t0) / DRIFT * 10))

    def unix(self, rt):
        return self.t0 + rt / 10 * DRIFT

    def ev(self, tag, name, rt, decoded, cap=None):
        self.n += 1
        self.con.execute("INSERT INTO events (serial, tag, name, ring_timestamp, body, decoded_json,"
                         " captured_unix) VALUES (?,?,?,?,?,?,?)",
                         (SERIAL, tag, name, int(rt), self.n.to_bytes(4, "little"),
                          json.dumps(decoded), int(cap or self.unix(rt) + 60)))

    def time_syncs(self, start, end, every=900, noise=120):
        t = start
        while t < end:
            rt = self.rt(t)
            self.ev(66, "time_sync", rt, {"unix_time": int(t + self.rnd.uniform(-noise, noise))})
            t += every

    def night(self, bed, wake, hr_base=62.0, hr_boost=0.0, temp=35.2, rr=14.0):
        """Nuit simulée : FC en « hamac » (+ hr_boost), battements modulés par la
        respiration (arythmie sinusale à rr cycles/min), bins 5 min, mouvement,
        température, fenêtre bedtime_period."""
        rnd = self.rnd
        dur = wake - bed
        # battements (tag 96) : paquets de 6, horodatage arrondi alterné
        t, beats, pkt = bed, [], []
        while t < wake:
            frac = (t - bed) / dur
            hr = hr_base + hr_boost + 8 * (frac - 0.35) ** 2 * 4 - 2 + rnd.gauss(0, 0.8)
            ibi = 60000 / hr * (1 + 0.04 * math.sin(2 * math.pi * rr / 60 * (t - bed)))
            ibi = int(round(ibi))
            t += ibi / 1000
            pkt.append(ibi)
            beats.append((t, 60000 / ibi))
            if len(pkt) == 6:
                jitter = rnd.choice((-1.1, 1.1))
                self.ev(96, "ibi_and_amplitude_event", self.rt(t + jitter),
                        {"ibi_ms": pkt, "amplitude": [800] * 6, "hr_bpm": [60000 // x for x in pkt]})
                pkt = []
        # bins hrv_event : 6 × 5 min, horodatés à la FIN
        b = bed
        while b + 1800 <= wake:
            hrs, rms = [], []
            for k in range(6):
                s, e = b + k * 300, b + (k + 1) * 300
                v = [h for tt, h in beats if s <= tt < e]
                hrs.append(int(round(sum(v) / len(v))) if v else 0)
                rms.append(int(35 - hr_boost + rnd.gauss(0, 4)) if v else 0)
            self.ev(93, "hrv_event", self.rt(b + 1800), {"hr_bpm": hrs, "rmssd_ms": rms, "interval_min": 5})
            b += 1800
        # mouvement 30 s (tag 114) : quasi immobile, quelques mouvements francs
        t = bed
        while t < wake:
            m = 0.1 + abs(rnd.gauss(0, 0.05))
            if rnd.random() < 0.03:
                m = rnd.uniform(5, 80)
            self.ev(114, "sleep_acm_period", self.rt(t + 30), {"acm_mad": [m / 2, m, m / 2, 0.01, 0.01, 0.0]})
            t += 30
        # température de sommeil (tag 117) toutes les ~5 min
        t = bed + 600
        while t < wake:
            self.ev(117, "sleep_temp_event", self.rt(t),
                    {"temps_c": [round(temp + rnd.gauss(0, 0.05), 2) for _ in range(6)]})
            t += 300
        self.ev(118, "bedtime_period", self.rt(wake + 600),
                {"bedtime_start_ds": self.rt(bed), "bedtime_end_ds": self.rt(wake)})

    def day(self, start, end, met_base=1.3):
        """Journée : MET (13 bins d'1 min alignés sur la fin), température cutanée."""
        t = start
        while t + 780 <= end:
            met = [round(max(0.9, met_base + self.rnd.gauss(0, 0.4)), 1) for _ in range(13)]
            if self.rnd.random() < 0.08:
                met = [4.5] * 13
            self.ev(80, "activity_information", self.rt(t + 780), {"state": 1, "met": met})
            self.ev(70, "temp_event", self.rt(t + 400), {"temps_c": [33.5, 34.0, 35.5]})
            t += 780

    def close(self):
        self.con.commit()
        self.con.close()


def build(path, nights=5, sick_last=False, start=None):
    """Base de `nights` nuits consécutives (la dernière éventuellement « malade »)."""
    start = start or datetime(2026, 9, 1, 12, 0)
    t0 = start.timestamp() - 3600
    ring = Ring(path, t0)
    ring.time_syncs(t0 + 60, t0 + (nights + 1) * 86400)
    for i in range(nights):
        d = start + timedelta(days=i)
        ring.day(d.timestamp(), (d + timedelta(hours=11)).timestamp())
        bed = (d + timedelta(hours=11, minutes=30)).timestamp()     # 23:30
        wake = bed + 7.5 * 3600
        sick = sick_last and i == nights - 1
        ring.night(bed, wake, hr_boost=15 if sick else 0, temp=36.4 if sick else 35.2,
                   rr=17 if sick else 14)
    ring.close()
    return ring
