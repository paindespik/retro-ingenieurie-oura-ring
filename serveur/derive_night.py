#!/usr/bin/env python3
"""derive_night.py — dérivations locales (job nightly sur le serveur).

Lit oura.db (brut open_oura, lecture seule) et écrit derived.db :

  sleep_scores    une ligne par nuit : durées, stades, efficacité, latence,
                  WASO, réveils, FC la plus basse (et son heure), FC moyenne,
                  HRV moyen/max, Recovery Index, fréquence respiratoire
                  (expérimentale), température et écart à la ligne de base,
                  sous-scores et score (estimé)
  night_staging   hypnogramme 30 s (heuristique ouverte, cf. staging.py)
  night_series    séries 5 min pour les graphes (FC, HRV, respiration, mouvement, T°)
  baselines       lignes de base personnelles (nuits antérieures à la dernière)
  readiness       contributeurs de récupération + signes de tension, par jour
  activity_daily  activité par jour civil (MET, inactivité, calories estimées)
  llm_briefings   résumé LLM de la dernière nuit (régénéré si ses entrées changent)

Définitions alignées sur la documentation Oura quand elle existe :
- FC la plus basse = minimum des moyennes glissantes 10 min (bins 5 min de
  l'anneau) pendant le sommeil ; HRV = moyenne des RMSSD 5 min (+ max) ;
- Recovery Index = sommeil restant après la FC la plus basse (optimal ≥ 6 h) ;
- écart de température = nuit − médiane des nuits précédentes (≤ 30),
  « provisoire » avant 14 nuits (Oura : ~2 semaines de calibrage).

Tout est calculé localement, sans modèle propriétaire ; les scores sont des
ESTIMATIONS (staging heuristique, pondérations publiques ou locales).

Usage :
  derive_night.py --db oura.db --derived derived.db [--briefing] [--all]
"""
import argparse
import bisect
import hashlib
import json
import os
import sqlite3
import statistics
import sys
import time
from datetime import datetime, timedelta

import oura_core as oc
import staging

VERSION = 2                      # incrémenter → recalcul de toutes les nuits
LLM_URL = os.environ.get("OURA_LLM_URL", "http://127.0.0.1:8012/v1/chat/completions")
LLM_MODEL = os.environ.get("OURA_LLM_MODEL", "qwen3.5-4b")
SCORE_SOURCE = ("sous-scores : anchors publiés (dmturner44/oura_sleep_score_algo) + "
                "restfulness locale ; poids Oura 35/15/10/10/10/10/10 — estimation")
SLEEP_NEED_H = 8.0               # milieu de la plage NSF 7–9 h (adultes)
TEMP_BASE_NIGHTS = 30
BASE_RELIABLE_N = 14


# ---------------------------------------------------------------- utilitaires

def r(x, nd=1):
    return None if x is None else round(x, nd)


def local_hours(unix):
    d = datetime.fromtimestamp(unix)
    return d.hour + d.minute / 60 + d.second / 3600


# ---------------------------------------------------------------- température

def nightly_temperature_centi(samples):
    """Port ecore nightly_temperature_calculate (ported/temperature.rs) :
    médiane glissante sur 7 échantillons, maxima par fenêtres de 30 à faible
    amplitude, puis minimum de ces maxima (niveau stable de la nuit)."""
    WINDOW, RANGE, MIN_W = 30, 250, 4
    ring = [0] * 7
    idx = 0
    maxima = []
    win_min, win_max = 0xFFFFFFFF, 0
    for i, s in enumerate(samples):
        ring[idx] = s
        idx = 0 if idx == 6 else idx + 1
        m = sorted(ring)[3]
        if m != 0:
            win_min = min(win_min, m)
            win_max = max(win_max, m)
        if (i + 1) % WINDOW == 0:
            if 0 < win_max and win_max - win_min < RANGE:
                maxima.append(win_max)
            win_min, win_max = 0xFFFFFFFF, 0
    if len(maxima) < MIN_W:
        return None
    return min(maxima)


def temp_samples(con, s_ds, e_ds, charging):
    """(rt, °C) de température CUTANÉE pendant la nuit, hors charge et hors
    bornes : sleep_temp_event (tag 117, dédié au sommeil) ; à défaut, le
    capteur cutané [0] de temp_event (tag 70). Les deux autres capteurs de
    temp_event (quantifié / interne) ne sont jamais mélangés à la peau."""
    out = []
    for rt, dj in oc.rows(con, "SELECT ring_timestamp, decoded_json FROM events WHERE tag=?"
                               " AND ring_timestamp>=? AND ring_timestamp<? ORDER BY ring_timestamp",
                          (oc.T_SLEEP_TEMP, s_ds, e_ds)):
        if oc.in_intervals(rt, charging):
            continue
        for t in (oc.jget(dj) or {}).get("temps_c") or []:
            if oc.SKIN_TEMP_MIN_C <= t <= oc.SKIN_TEMP_MAX_C:
                out.append((rt, t))
    if len(out) < 60:
        out = []
        for rt, dj in oc.rows(con, "SELECT ring_timestamp, decoded_json FROM events WHERE tag=?"
                                   " AND ring_timestamp>=? AND ring_timestamp<? ORDER BY ring_timestamp",
                              (oc.T_TEMP, s_ds, e_ds)):
            t = oc.skin_temp(oc.jget(dj))
            if t is not None and not oc.in_intervals(rt, charging):
                out.append((rt, t))
    return out


# ---------------------------------------------------------------- sous-scores
# Anchors publiés par dmturner44/oura_sleep_score_algo (constantes conservées,
# branches corrigées là où le code public oubliait la multiplication).

def subscore_total_sleep(tts_s):
    return max(0.0, min(100.0, 100.0 / (9 * 3600) * tts_s))


def subscore_rem(rem_s):
    if rem_s < 6690:
        return max(0.0, 95.0 / 6690 * rem_s)
    return min(100.0, 95 + 5 / (8820 - 6690) * (rem_s - 6690))


def subscore_deep(deep_s):
    if deep_s < 5580:
        return max(0.0, 95.0 / 5580 * deep_s)
    return min(100.0, 95 + 5 / (8670 - 5580) * (deep_s - 5580))


def subscore_efficiency(eff_pct):
    if eff_pct <= 90:
        return max(0.0, 37 + (95 - 37) / (90 - 65) * (eff_pct - 65))
    return min(100.0, 95 + 5 / (95 - 90) * (eff_pct - 90))


def subscore_latency(lat_s):
    if lat_s <= 900:
        return 59 + (100 - 59) / 900 * lat_s
    if lat_s <= 2670:
        return 100 - (100 - 22) / (2670 - 900) * (lat_s - 900)
    if lat_s <= 4350:
        return max(0.0, 22 - 22 / (4350 - 2670) * (lat_s - 2670))
    return 0.0


def subscore_timing(mid_h):
    """Milieu du sommeil (heure locale) : 100 jusqu'à 02:40, 0 à 05:43.
    Un milieu avant minuit compte comme précoce (100)."""
    sec = (mid_h - 24 if mid_h >= 12 else mid_h) * 3600
    if sec <= 9620:
        return 100.0
    if sec >= 20620:
        return 0.0
    return 100.0 * (20620 - sec) / (20620 - 9620)


def subscore_restfulness(waso_min, awakenings, restless_frac):
    """Formule locale et monotone (plus d'éveil ou d'agitation → note plus
    basse), ancrée sur les seuils NSF 2017 pour l'adulte : WASO < 20 min,
    ≤ 1 réveil de plus de 5 min. Remplace une régression publiée qui
    récompensait la fragmentation (coefficient positif sur le nombre de
    segments de sommeil)."""
    w = oc.lerp_score(waso_min, [(20, 100), (50, 60), (100, 0)])
    a = oc.lerp_score(awakenings, [(1, 100), (4, 40), (8, 0)])
    m = oc.lerp_score(restless_frac * 100, [(5, 100), (20, 50), (40, 0)])
    return 0.4 * w + 0.3 * a + 0.3 * m


WEIGHTS = {"total_sleep": 35, "restfulness": 15, "efficiency": 10,
           "latency": 10, "deep": 10, "rem": 10, "timing": 10}


# ---------------------------------------------------------------- nuit

def night_input_hash(con, night):
    s, e = night["start_ds"], night["end_ds"]
    cnt, mx = con.execute("SELECT COUNT(*), MAX(id) FROM events WHERE ring_timestamp>=?"
                          " AND ring_timestamp<?", (s - 36000, e + 36000)).fetchone()
    return hashlib.sha1(f"{VERSION}|{s}|{e}|{cnt}|{mx}".encode()).hexdigest()[:16]


def compute_night(con, axis, night, charging):
    S, E = night["start_ds"], night["end_ds"]
    n = max(1, int(round((E - S) / staging.EPOCH_DS)))
    mev = []
    for rt, dj in oc.rows(con, "SELECT ring_timestamp, decoded_json FROM events WHERE tag=?"
                               " AND ring_timestamp>=? AND ring_timestamp<?", (oc.T_ACM, S, E)):
        vals = (oc.jget(dj) or {}).get("acm_mad") or []
        if vals:
            mev.append((rt, max(vals)))
    beats = oc.clean_beats(oc.load_beats(con, S, E))
    stages, feats, capped = staging.stage_night(n, S, mev, beats)
    sm = staging.summarize(stages, feats["motion"])

    onset_ds = S + sm["onset"] * staging.EPOCH_DS
    end_ds = S + (sm["end"] + 1) * staging.EPOCH_DS
    total_s = sm["total"]
    in_bed_s = (E - S) / 10.0

    # --- FC / HRV : bins 5 min calculés par l'anneau, pendant le sommeil
    bins = oc.hrv_bins(con, S, E)
    sleep_bins = [b for b in bins if onset_ds <= b[0] < end_ds]
    hrs = [(c, h) for c, h, _ in sleep_bins if h is not None]
    rms = [x for _, _, x in sleep_bins if x is not None]
    hr_source = "anneau (hrv_event)"
    if len(hrs) < 6:               # repli : FC 5 min depuis les battements
        hr_source = "battements (tag 96)"
        hrs = []
        c = onset_ds
        bt = [t for t, _ in beats]
        while c + 3000 <= end_ds:
            a, b = bisect.bisect_left(bt, c), bisect.bisect_left(bt, c + 3000)
            ib = [x for _, x in beats[a:b]]
            if len(ib) >= 150:
                hrs.append((c + 1500, 60000.0 / (sum(ib) / len(ib))))
            c += 3000
    hr_mean = oc.mean([h for _, h in hrs])
    hr_low, hr_low_at = None, None
    for (c0, h0), (c1, h1) in zip(hrs, hrs[1:]):
        if c1 - c0 <= 3300:        # deux bins de 5 min consécutifs = 10 min
            v = (h0 + h1) / 2
            if hr_low is None or v < hr_low:
                hr_low, hr_low_at = v, (c0 + c1) / 2
    if hr_low is None and hrs:
        c, h = min(hrs, key=lambda p: p[1])
        hr_low, hr_low_at = float(h), c
    recovery_h = None
    if hr_low_at is not None:
        k = int((hr_low_at - S) // staging.EPOCH_DS)
        recovery_h = sum(staging.EPOCH_DS / 10 for s in stages[max(0, k):] if s != "awake") / 3600

    # RMSSD « maison » sur les battements : contrôle croisé seulement
    own_rm = []
    bt = [t for t, _ in beats]
    c = onset_ds
    while c + 3000 <= end_ds:
        a, b = bisect.bisect_left(bt, c), bisect.bisect_left(bt, c + 3000)
        seg = [x for _, x in beats[a:b]]
        if len(seg) >= 150:
            own_rm.append(oc.rmssd(seg))
        c += 3000

    # --- respiration (expérimental)
    resp = oc.resp_rate_windows(beats, onset_ds, end_ds)
    resp_rate = oc.median([v for _, v in resp]) if len(resp) >= 12 else None

    # --- température
    temps = temp_samples(con, S, E, charging)
    t_centi = nightly_temperature_centi([int(round(t * 100)) for _, t in temps])
    temp_mean = (t_centi / 100.0) if t_centi is not None else (
        oc.median([t for _, t in temps]) if len(temps) >= 60 else None)

    # --- timing : milieu du SOMMEIL (et non du lit)
    onset_u, end_u = axis(onset_ds), axis(end_ds)
    mid_h = local_hours((onset_u + end_u) / 2) if onset_u and end_u else None

    eff = 100.0 * total_s / in_bed_s if in_bed_s else 0.0
    sleep_epochs = sum(1 for s in stages[sm["onset"]:sm["end"] + 1] if s != "awake") or 1
    restless_frac = sm["restless"] / 30 / sleep_epochs
    subs = {
        "total_sleep": subscore_total_sleep(total_s),
        "rem": subscore_rem(sm["rem"]),
        "deep": subscore_deep(sm["deep"]),
        "efficiency": subscore_efficiency(eff),
        "latency": subscore_latency(sm["latency"]),
        "timing": subscore_timing(mid_h) if mid_h is not None else None,
        "restfulness": subscore_restfulness(sm["waso"] / 60, sm["awakenings"], restless_frac),
    }
    avail = {k: v for k, v in subs.items() if v is not None}
    wsum = sum(WEIGHTS[k] for k in avail)
    score = int(round(sum(WEIGHTS[k] * v for k, v in avail.items()) / wsum)) if wsum else None

    # --- séries 5 min pour les graphes
    series = {}

    def slot(ds):
        return int((ds - S) // 3000)
    for c, h, x in bins:
        d = series.setdefault(slot(c), {})
        d["hr"], d["rmssd"] = h, x
    for c, v in resp:
        series.setdefault(slot(c), {})["resp"] = round(v, 1)
    for i, m in enumerate(feats["motion"]):
        d = series.setdefault(i // 10, {})
        d["motion"] = max(d.get("motion", 0.0), m)
    tbuck = {}
    for rt, t in temps:
        tbuck.setdefault(slot(rt), []).append(t)
    for k, v in tbuck.items():
        series.setdefault(k, {})["temp"] = round(statistics.median(v), 2)
    series_rows = []
    for k in sorted(series):
        if 0 <= k < (E - S) / 3000 + 1:
            t = axis(S + k * 3000 + 1500)
            d = series[k]
            series_rows.append((int(t), d.get("hr"), d.get("rmssd"), d.get("resp"),
                                r(d.get("motion"), 2), d.get("temp")))

    row = {
        "night": night["night"],
        "score": score,
        "total": round(total_s / 3600, 2),
        "deep": round(sm["deep"] / 3600, 2),
        "rem": round(sm["rem"] / 3600, 2),
        "light": round(sm["light"] / 3600, 2),
        "awake": round(sm["awake"] / 3600, 2),
        "in_bed": round(in_bed_s / 3600, 2),
        "efficiency": round(eff, 1),
        "latency": round(sm["latency"] / 60, 1),
        "timing": r(mid_h, 2),
        "restfulness": round(subs["restfulness"], 1),
        "waso": round(sm["waso"] / 60, 1),
        "awakenings": sm["awakenings"],
        "hr_min": r(hr_low, 1),
        "hr_lowest_at": int(axis(hr_low_at)) if hr_low_at else None,
        "hr_mean": r(hr_mean, 1),
        "hrv_rmssd": r(oc.mean(rms), 1),
        "hrv_max": max(rms) if rms else None,
        "hrv_beats": r(oc.mean([x for x in own_rm if x]), 1),
        "hr_source": hr_source,
        "recovery_index": r(recovery_h, 2),
        "resp_rate": r(resp_rate, 1),
        "resp_n": len(resp),
        "temp_mean": r(temp_mean, 2),
        "temp_dev": None, "temp_n": 0, "temp_status": None,
        "movement": round(sm["restless"] / 60, 1),
        "spo2": None,
        "bedtime": datetime.fromtimestamp(night["start_unix"]).strftime("%H:%M"),
        "wake_time": datetime.fromtimestamp(night["end_unix"]).strftime("%H:%M"),
        "start_unix": int(night["start_unix"]), "stop_unix": int(night["end_unix"]),
        "onset_unix": int(onset_u) if onset_u else None,
        "end_unix": int(end_u) if end_u else None,
        "staging_source": "heuristique v2" + (" (plafond %s)" % "+".join(capped) if capped else ""),
        "subscores": json.dumps({k: r(v, 1) for k, v in subs.items()}),
        "score_source": SCORE_SOURCE,
        "axis_mode": axis.mode if night["start_ds"] >= (axis.first_anchor_ds or 0) else "extrapolé",
        "version": VERSION,
        "ts": int(time.time()),
        "_stages": stages,
        "_series": series_rows,
    }
    return row


# ---------------------------------------------------------------- schéma

SLEEP_COLS = [
    ("night", "TEXT PRIMARY KEY"), ("score", "REAL"), ("total", "REAL"), ("deep", "REAL"),
    ("rem", "REAL"), ("light", "REAL"), ("awake", "REAL"), ("in_bed", "REAL"),
    ("efficiency", "REAL"), ("latency", "REAL"), ("timing", "REAL"), ("restfulness", "REAL"),
    ("waso", "REAL"), ("awakenings", "INTEGER"), ("hr_min", "REAL"), ("hr_lowest_at", "INTEGER"),
    ("hr_mean", "REAL"), ("hrv_rmssd", "REAL"), ("hrv_max", "REAL"), ("hrv_beats", "REAL"),
    ("hr_source", "TEXT"), ("recovery_index", "REAL"), ("resp_rate", "REAL"), ("resp_n", "INTEGER"),
    ("temp_mean", "REAL"), ("temp_dev", "REAL"), ("temp_n", "INTEGER"), ("temp_status", "TEXT"),
    ("movement", "REAL"), ("spo2", "REAL"), ("bedtime", "TEXT"), ("wake_time", "TEXT"),
    ("start_unix", "INTEGER"), ("stop_unix", "INTEGER"), ("onset_unix", "INTEGER"),
    ("end_unix", "INTEGER"), ("staging_source", "TEXT"), ("subscores", "TEXT"),
    ("score_source", "TEXT"), ("axis_mode", "TEXT"), ("version", "INTEGER"),
    ("input_hash", "TEXT"), ("ts", "INTEGER"),
]


def ensure_schema(con):
    con.execute("CREATE TABLE IF NOT EXISTS sleep_scores (%s)"
                % ", ".join(f"{c} {t}" for c, t in SLEEP_COLS))
    have = {x[1] for x in con.execute("PRAGMA table_info(sleep_scores)")}
    for c, t in SLEEP_COLS:
        if c not in have:
            con.execute(f"ALTER TABLE sleep_scores ADD COLUMN {c} {t.replace(' PRIMARY KEY', '')}")
    con.executescript("""
        CREATE TABLE IF NOT EXISTS night_staging (
            night TEXT, epoch INTEGER, stage TEXT, PRIMARY KEY (night, epoch));
        CREATE TABLE IF NOT EXISTS night_series (
            night TEXT, t INTEGER, hr REAL, rmssd REAL, resp REAL, motion REAL, temp REAL,
            PRIMARY KEY (night, t));
        CREATE TABLE IF NOT EXISTS llm_briefings (
            night TEXT, model TEXT, text TEXT, ts INTEGER);
        CREATE TABLE IF NOT EXISTS baselines (
            metric TEXT PRIMARY KEY, mean REAL, sd REAL, updated_unix INTEGER);
        CREATE TABLE IF NOT EXISTS readiness (
            day TEXT PRIMARY KEY, score REAL, contributors TEXT, tension TEXT,
            n_base INTEGER, provisional INTEGER, ts INTEGER);
        CREATE TABLE IF NOT EXISTS activity_daily (
            day TEXT PRIMARY KEY, worn_min INTEGER, sleep_min INTEGER, inactive_min INTEGER,
            low_min INTEGER, medium_min INTEGER, high_min INTEGER, met_min_mh REAL,
            sedentary_bouts INTEGER, active_kcal REAL, easy_day INTEGER,
            score REAL, contributors TEXT, partial INTEGER, ts INTEGER);
        CREATE TABLE IF NOT EXISTS tags (
            id INTEGER PRIMARY KEY AUTOINCREMENT, day TEXT NOT NULL, kind TEXT NOT NULL,
            note TEXT, created_unix INTEGER);
        CREATE TABLE IF NOT EXISTS user_profile (
            id INTEGER PRIMARY KEY CHECK (id=1), age_years INTEGER, height_cm INTEGER,
            weight_kg REAL, sex TEXT, updated_unix INTEGER);
    """)
    for tab, col, typ in (("llm_briefings", "input_hash", "TEXT"), ("baselines", "median", "REAL"),
                          ("baselines", "n", "INTEGER")):
        if col not in {x[1] for x in con.execute(f"PRAGMA table_info({tab})")}:
            con.execute(f"ALTER TABLE {tab} ADD COLUMN {col} {typ}")


def write_night(con, row, input_hash):
    cols = [c for c, _ in SLEEP_COLS if c != "input_hash"]
    vals = [row.get(c) for c in cols] + [input_hash]
    con.execute("INSERT OR REPLACE INTO sleep_scores (%s, input_hash) VALUES (%s)"
                % (", ".join(cols), ", ".join("?" * len(vals))), vals)
    con.execute("DELETE FROM night_staging WHERE night=?", (row["night"],))
    con.executemany("INSERT INTO night_staging (night, epoch, stage) VALUES (?,?,?)",
                    [(row["night"], i, s) for i, s in enumerate(row["_stages"])])
    con.execute("DELETE FROM night_series WHERE night=?", (row["night"],))
    con.executemany("INSERT OR REPLACE INTO night_series (night, t, hr, rmssd, resp, motion, temp)"
                    " VALUES (?,?,?,?,?,?,?)", [(row["night"], *s) for s in row["_series"]])


# ---------------------------------------------------------------- lignes de base

def nights_table(der):
    der.row_factory = sqlite3.Row
    out = [dict(x) for x in der.execute("SELECT * FROM sleep_scores ORDER BY night")]
    der.row_factory = None
    return out


def update_temperature_deviation(der, nights):
    """Écart de température = nuit − médiane des nuits STRICTEMENT antérieures
    (≤ 30). Aucune valeur avant 3 nuits ; « provisoire » avant 14."""
    for i, n in enumerate(nights):
        prev = [p["temp_mean"] for p in nights[max(0, i - TEMP_BASE_NIGHTS):i]
                if p["temp_mean"] is not None]
        dev, status = None, "calibrage"
        if n["temp_mean"] is not None and len(prev) >= 3:
            dev = round(n["temp_mean"] - statistics.median(prev), 2)
            status = "fiable" if len(prev) >= BASE_RELIABLE_N else "provisoire"
        n["temp_dev"], n["temp_n"], n["temp_status"] = dev, len(prev), status
        der.execute("UPDATE sleep_scores SET temp_dev=?, temp_n=?, temp_status=? WHERE night=?",
                    (dev, len(prev), status, n["night"]))


BASELINE_METRICS = ("hr_min", "hr_mean", "hrv_rmssd", "resp_rate", "temp_mean", "total",
                    "efficiency", "score", "deep", "rem", "waso", "recovery_index")


def base_stats(vals):
    vals = [v for v in vals if v is not None]
    if not vals:
        return None
    return {"mean": statistics.fmean(vals), "sd": statistics.pstdev(vals) if len(vals) > 1 else 0.0,
            "median": statistics.median(vals), "n": len(vals)}


def update_baselines(der, nights, now):
    """Lignes de base affichées : nuits antérieures à la dernière (≤ 60)."""
    der.execute("DELETE FROM baselines")
    prev = nights[:-1][-60:]
    for m in BASELINE_METRICS:
        b = base_stats([p.get(m) for p in prev])
        if b:
            der.execute("INSERT INTO baselines (metric, mean, sd, median, n, updated_unix)"
                        " VALUES (?,?,?,?,?,?)",
                        (m, round(b["mean"], 3), round(b["sd"], 3), round(b["median"], 3), b["n"], now))


# ---------------------------------------------------------------- récupération

def readiness_for(i, nights, activity):
    """Contributeurs de type Readiness (Oura) pour le jour de la nuit i.
    Chaque contributeur 0–100, None si données insuffisantes."""
    n = nights[i]
    prev = nights[max(0, i - 60):i]
    c = {}

    b = base_stats([p["hr_min"] for p in prev])
    if n["hr_min"] is not None and b and b["n"] >= 3:
        d = n["hr_min"] - b["mean"]
        c["rhr"] = {"score": oc.lerp_score(d, [(-15, 50), (-10, 85), (-5, 100), (0, 100), (3, 85),
                                              (5, 65), (10, 30), (15, 10)]),
                    "value": n["hr_min"], "baseline": round(b["mean"], 1), "delta": round(d, 1)}

    last14 = [p["hrv_rmssd"] for p in nights[max(0, i - 13):i + 1] if p["hrv_rmssd"] is not None]
    long_ = [p["hrv_rmssd"] for p in nights[max(0, i - 90):i + 1] if p["hrv_rmssd"] is not None]
    if len(long_) >= 5 and last14:
        w = list(range(1, len(last14) + 1))
        recent = sum(a * b_ for a, b_ in zip(last14, w)) / sum(w)
        ratio = recent / statistics.fmean(long_)
        c["hrv_balance"] = {"score": oc.lerp_score(ratio, [(0.6, 15), (0.75, 45), (0.9, 75),
                                                           (1.0, 95), (1.05, 100)]),
                            "value": round(recent, 1), "baseline": round(statistics.fmean(long_), 1),
                            "delta": round((ratio - 1) * 100, 1)}

    if n["temp_dev"] is not None:
        c["temperature"] = {"score": oc.lerp_score(abs(n["temp_dev"]), [(0.2, 100), (0.5, 85), (1.0, 50),
                                                                       (1.5, 25), (2.5, 5)]),
                            "value": n["temp_dev"], "status": n["temp_status"]}

    if n["recovery_index"] is not None:
        c["recovery_index"] = {"score": oc.lerp_score(n["recovery_index"], [(0, 10), (2, 35), (4, 60), (6, 100)]),
                               "value": n["recovery_index"]}

    c["sleep"] = {"score": oc.lerp_score(n["total"], [(4, 20), (5, 40), (6, 60), (7, 85), (8, 100)]),
                  "value": n["total"]}

    win = [p["total"] for p in nights[max(0, i - 13):i + 1] if p["total"] is not None]
    if len(win) >= 3:
        w = list(range(1, len(win) + 1))
        avg = sum(a * b_ for a, b_ in zip(win, w)) / sum(w)
        deficit = SLEEP_NEED_H - avg
        c["sleep_balance"] = {"score": oc.lerp_score(deficit, [(0, 100), (0.5, 85), (1, 70), (2, 45), (3, 20)]),
                              "value": round(avg, 2), "need": SLEEP_NEED_H}

    mids = [p["timing"] for p in nights[max(0, i - 13):i + 1] if p["timing"] is not None]
    if len(mids) >= 5:
        adj = [(m - 24 if m >= 12 else m) * 60 for m in mids]
        sd = statistics.pstdev(adj)
        c["sleep_regularity"] = {"score": oc.lerp_score(sd, [(30, 100), (60, 75), (90, 50), (120, 25)]),
                                 "value": round(sd)}

    day_before = (datetime.strptime(n["night"], "%Y-%m-%d") - timedelta(days=1)).strftime("%Y-%m-%d")
    a = activity.get(day_before)
    if a and not a["partial"]:
        s1 = oc.lerp_score(a["inactive_min"] / 60, [(8, 100), (10, 80), (12, 60), (14, 40)])
        s2 = oc.lerp_score(a["met_min_mh"], [(0, 50), (50, 75), (100, 100), (400, 100), (800, 80)])
        c["previous_day_activity"] = {"score": (s1 + s2) / 2, "value": a["met_min_mh"],
                                      "inactive_min": a["inactive_min"]}

    for k in c:
        c[k]["score"] = r(c[k]["score"], 0)
        c[k]["rating"] = oc.rating(c[k]["score"])
    scores = [x["score"] for x in c.values() if x["score"] is not None]
    score = round(statistics.fmean(scores)) if len(scores) >= 5 else None
    return score, c, len(prev)


def tension_for(i, nights):
    """Signes de tension (analogue non diagnostique du Symptom Radar Oura) :
    écarts simultanés de la FC la plus basse, de la FC moyenne, du HRV, de la
    température et de la respiration par rapport aux nuits antérieures."""
    n = nights[i]
    prev = nights[max(0, i - 60):i]
    sig = []

    def chk(metric, label, unit, cond, strong=None):
        b = base_stats([p.get(metric) for p in prev])
        v = n.get(metric)
        if v is None or not b or b["n"] < 3:
            return
        d = v - b["mean"]
        if cond(d, b):
            sig.append({"metric": metric, "label": label, "value": v, "baseline": round(b["mean"], 1),
                        "delta": round(d, 2), "unit": unit,
                        "strong": bool(strong and strong(d, b))})

    chk("hr_min", "FC la plus basse", "bpm", lambda d, b: d >= max(5, 2 * b["sd"]), lambda d, b: d >= 10)
    chk("hr_mean", "FC moyenne de nuit", "bpm", lambda d, b: d >= 8, lambda d, b: d >= 15)
    chk("hrv_rmssd", "HRV moyen", "ms", lambda d, b: d <= -0.25 * b["mean"], lambda d, b: d <= -0.4 * b["mean"])
    if (n.get("resp_n") or 0) >= 20:
        chk("resp_rate", "Fréquence respiratoire", "/min", lambda d, b: d >= 2, lambda d, b: d >= 3)
    if n.get("temp_dev") is not None and n["temp_dev"] >= 0.5:
        sig.append({"metric": "temp_dev", "label": "Température", "value": n["temp_dev"], "baseline": 0.0,
                    "delta": n["temp_dev"], "unit": "°C", "strong": n["temp_dev"] >= 1.0})
    strong = sum(1 for s in sig if s["strong"])
    level = "aucun" if not sig else ("marqués" if len(sig) >= 2 and (strong or len(sig) >= 3) else "mineurs")
    return {"level": level, "signals": sig, "n_base": len(prev),
            "provisional": len(prev) < BASE_RELIABLE_N}


def update_readiness(der, nights, activity, now):
    for i, n in enumerate(nights):
        score, contrib, nb = readiness_for(i, nights, activity)
        tension = tension_for(i, nights)
        der.execute("INSERT OR REPLACE INTO readiness (day, score, contributors, tension, n_base,"
                    " provisional, ts) VALUES (?,?,?,?,?,?,?)",
                    (n["night"], score, json.dumps(contrib, ensure_ascii=False),
                     json.dumps(tension, ensure_ascii=False), nb, int(nb < BASE_RELIABLE_N), now))


# ---------------------------------------------------------------- activité

def profile_weight(der, con):
    row = der.execute("SELECT weight_kg FROM user_profile WHERE id=1").fetchone()
    if row and row[0]:
        return float(row[0]), "profil"
    ui = con.execute("SELECT decoded_json FROM events WHERE tag=? ORDER BY ring_timestamp DESC LIMIT 1",
                     (oc.T_USER_INFO,)).fetchone()
    w = (oc.jget(ui[0]) or {}).get("weight_kg") if ui else None
    return (float(w), "anneau") if w else (70.0, "défaut")


def worn_minutes(con, axis, t0, t1, charging):
    """Minutes où l'anneau est au doigt : température de peau > 30 °C à ±5 min
    (au repos sur une table, l'anneau est à la température de la pièce)."""
    s_ds, e_ds = axis.to_ds(t0 - 600), axis.to_ds(t1 + 600)
    hot = set()
    for rt, cap, dj in oc.rows(con, "SELECT ring_timestamp, captured_unix, decoded_json FROM events"
                                    " WHERE tag=? AND ring_timestamp>=? AND ring_timestamp<?",
                               (oc.T_TEMP, s_ds, e_ds)):
        if oc.in_intervals(rt, charging):
            continue
        v = oc.skin_temp(oc.jget(dj))
        if v is not None and v > 30.0:
            t = axis(rt, cap)
            if t is not None:
                m = int(t // 60)
                for k in range(m - 5, m + 6):
                    hot.add(k * 60)
    return hot


def compute_activity_day(con, axis, day, sleep_windows, charging, weight):
    t0 = datetime.strptime(day, "%Y-%m-%d").timestamp()
    t1 = t0 + 86400
    mets = oc.met_minutes(con, axis, t0, t1)
    worn = worn_minutes(con, axis, t0, t1, charging)
    sleep = {m for m in mets if any(a <= m < b for a, b in sleep_windows)}
    awake = sorted(m for m in mets if m in worn and m not in sleep)
    inactive = [m for m in awake if mets[m] < 1.5]
    low = sum(1 for m in awake if 1.5 <= mets[m] < 3)
    med = sum(1 for m in awake if 3 <= mets[m] < 6)
    high = sum(1 for m in awake if mets[m] >= 6)
    met_mh = sum(mets[m] for m in awake if mets[m] >= 3)
    kcal = sum((mets[m] - 1) * 3.5 * weight / 200 for m in awake if mets[m] >= 1.5)
    # périodes sédentaires > 50 min (trous ≤ 2 min tolérés)
    bouts, run, last = 0, 0, None
    for m in inactive:
        if last is not None and m - last <= 180:
            run += (m - last) // 60
        else:
            if run > 50:
                bouts += 1
            run = 1
        last = m
    if run > 50:
        bouts += 1
    return {"day": day, "worn_min": len([m for m in mets if m in worn]), "sleep_min": len(sleep),
            "inactive_min": len(inactive), "low_min": low, "medium_min": med, "high_min": high,
            "met_min_mh": round(met_mh, 1), "sedentary_bouts": bouts, "active_kcal": round(kcal),
            "easy_day": int(high <= 15 and med + high <= 85),
            "partial": int(len(awake) < 600 or t1 > time.time())}


def activity_contributors(day, activity):
    a = activity[day]
    c = {"stay_active": {"score": oc.lerp_score(a["inactive_min"] / 60, [(5, 100), (8, 85), (12, 55), (16, 20)]),
                         "value": a["inactive_min"]},
         "move_every_hour": {"score": oc.lerp_score(a["sedentary_bouts"], [(0, 100), (1, 90), (3, 65), (6, 30)]),
                             "value": a["sedentary_bouts"]}}
    d0 = datetime.strptime(day, "%Y-%m-%d")
    week = [activity.get((d0 - timedelta(days=k)).strftime("%Y-%m-%d")) for k in range(7)]
    week = [w for w in week if w and w["worn_min"] >= 600]
    if len(week) >= 3:
        freq = sum(1 for w in week if w["met_min_mh"] >= 100)
        vol = sum(w["met_min_mh"] for w in week) * 7 / len(week)
        easy = sum(w["easy_day"] for w in week)
        c["training_frequency"] = {"score": oc.lerp_score(freq, [(0, 20), (1, 45), (2, 70), (3, 95), (4, 100)]),
                                   "value": freq, "days": len(week)}
        c["training_volume"] = {"score": oc.lerp_score(vol, [(0, 20), (750, 60), (1500, 85), (2000, 100)]),
                                "value": round(vol), "days": len(week)}
        c["recovery_time"] = {"score": oc.lerp_score(easy, [(0, 30), (1, 85), (2, 100)]),
                              "value": easy, "days": len(week)}
    for k in c:
        c[k]["score"] = r(c[k]["score"], 0)
        c[k]["rating"] = oc.rating(c[k]["score"])
    sc = [x["score"] for x in c.values()]
    return (round(statistics.fmean(sc)) if len(sc) >= 5 else None), c


def update_activity(con, der, axis, nights_raw, charging, recompute_all, now):
    known = {x[0]: x[1] for x in der.execute("SELECT day, partial FROM activity_daily")}
    first = con.execute("SELECT MIN(captured_unix) FROM events WHERE tag=?", (oc.T_ACTIVITY,)).fetchone()[0]
    if not first:
        return {}
    d = datetime.fromtimestamp(first).date()
    today = datetime.fromtimestamp(now).date()
    weight, _ = profile_weight(der, con)
    windows = [(n["start_unix"], n["end_unix"]) for n in nights_raw]
    windows += oc.short_rest_periods(con, axis)
    while d <= today:
        day = d.strftime("%Y-%m-%d")
        if recompute_all or day not in known or known[day] or (today - d).days <= 1:
            a = compute_activity_day(con, axis, day, windows, charging, weight)
            der.execute("INSERT OR REPLACE INTO activity_daily (day, worn_min, sleep_min, inactive_min,"
                        " low_min, medium_min, high_min, met_min_mh, sedentary_bouts, active_kcal, easy_day,"
                        " partial, ts) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (day, a["worn_min"], a["sleep_min"], a["inactive_min"], a["low_min"], a["medium_min"],
                         a["high_min"], a["met_min_mh"], a["sedentary_bouts"], a["active_kcal"],
                         a["easy_day"], a["partial"], now))
        d += timedelta(days=1)
    der.row_factory = sqlite3.Row
    act = {x["day"]: dict(x) for x in der.execute("SELECT * FROM activity_daily")}
    der.row_factory = None
    for day in act:
        score, contrib = activity_contributors(day, act)
        der.execute("UPDATE activity_daily SET score=?, contributors=? WHERE day=?",
                    (score, json.dumps(contrib, ensure_ascii=False), day))
        act[day]["score"], act[day]["contributors"] = score, contrib
    return act


# ---------------------------------------------------------------- briefing LLM

PLAUSIBLE = {"hr_min": (30, 120), "hr_mean": (30, 140), "hrv_rmssd": (3, 250), "resp_rate": (5, 35),
             "temp_mean": (25, 40), "temp_dev": (-4, 4), "total": (0, 16), "efficiency": (0, 100)}


def briefing_context(der, night, days=14):
    nights = [n for n in nights_table(der) if n["night"] <= night][-days:]
    keys = ["night", "score", "total", "deep", "rem", "light", "efficiency", "latency", "waso",
            "awakenings", "timing", "hr_min", "hr_mean", "hrv_rmssd", "recovery_index", "resp_rate",
            "temp_mean", "temp_dev", "temp_status"]
    out = []
    for n in nights:
        o = {}
        for k in keys:
            v = n.get(k)
            lo_hi = PLAUSIBLE.get(k)
            if v is None or (lo_hi and not lo_hi[0] <= v <= lo_hi[1]):
                continue
            o[k] = v
        out.append(o)
    rd = der.execute("SELECT score, contributors, tension FROM readiness WHERE day=?", (night,)).fetchone()
    tags = [dict(zip(("day", "kind", "note"), t)) for t in der.execute(
        "SELECT day, kind, note FROM tags WHERE day>=? AND day<=? ORDER BY day",
        ((datetime.strptime(night, "%Y-%m-%d") - timedelta(days=2)).strftime("%Y-%m-%d"), night))]
    ctx = {"nuits": out,
           "recuperation": {"score": rd[0], "contributeurs": json.loads(rd[1] or "{}"),
                            "signes_de_tension": json.loads(rd[2] or "{}")} if rd else None,
           "journal": tags}
    return json.dumps(ctx, ensure_ascii=False)


BRIEFING_SYSTEM = (
    "Tu rédiges le résumé matinal des données de sommeil d'un utilisateur d'anneau Oura "
    "(données locales, sans cloud). Tu reçois en JSON : ses dernières nuits (durées, stades estimés, "
    "efficacité, latence, WASO, FC la plus basse, FC moyenne, HRV moyen, Recovery Index, fréquence "
    "respiratoire expérimentale, température et écart à sa ligne de base), les contributeurs de "
    "récupération, les signes de tension et son journal (tags).\n"
    "Règles :\n"
    "- français, 5 à 8 lignes, ton factuel et bienveillant ;\n"
    "- n'utilise QUE les chiffres du contexte ; si une donnée manque, dis-le ;\n"
    "- n'affirme aucune cause : propose au plus des facteurs POSSIBLES, et seulement s'ils sont "
    "cohérents avec les données ou le journal ;\n"
    "- aucun diagnostic médical ; les stades et scores sont des estimations non validées ;\n"
    "- si temp_status vaut 'calibrage' ou 'provisoire', précise que l'écart de température est peu fiable ;\n"
    "- si les signes de tension sont 'marqués', conseille du repos, de noter les symptômes, de mesurer "
    "sa température avec un thermomètre en cas de malaise, et de consulter un médecin si cela persiste "
    "ou en cas de symptômes inquiétants (douleur thoracique, essoufflement : 15 ou 112) ;\n"
    "- termine par une suggestion concrète et raisonnable."
)


def llm_briefing(der, night, ctx, ctx_hash):
    import urllib.request
    payload = {
        "model": LLM_MODEL,
        "messages": [{"role": "system", "content": BRIEFING_SYSTEM + "\nContexte :\n" + ctx},
                     {"role": "user", "content": f"Rédige le résumé de la nuit du {night}."}],
        "temperature": 0.3, "max_tokens": 800,
        "chat_template_kwargs": {"enable_thinking": False},
    }
    req = urllib.request.Request(LLM_URL, data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=240) as resp:
            text = (json.load(resp)["choices"][0]["message"].get("content") or "").strip()
    except Exception as e:  # noqa: BLE001
        print(f"nightly: briefing LLM indisponible : {e}", file=sys.stderr)
        return None
    if not text:
        return None
    der.execute("INSERT INTO llm_briefings (night, model, text, ts, input_hash) VALUES (?,?,?,?,?)",
                (night, LLM_MODEL, text, int(time.time()), ctx_hash))
    return text


def maybe_briefing(der, latest, now, min_age_s=2700):
    """Régénère le résumé quand ses entrées ont changé, une fois la nuit
    stabilisée (fin de la fenêtre il y a ≥ 45 min : l'anneau révise son
    analyse pendant ~1 h après le réveil)."""
    if now - (latest.get("stop_unix") or now) < min_age_s:
        return
    ctx = briefing_context(der, latest["night"])
    h = hashlib.sha1(ctx.encode()).hexdigest()[:16]
    last = der.execute("SELECT input_hash FROM llm_briefings WHERE night=? ORDER BY ts DESC LIMIT 1",
                       (latest["night"],)).fetchone()
    if last and last[0] == h:
        return
    txt = llm_briefing(der, latest["night"], ctx, h)
    if txt:
        print(f"nightly: briefing LLM {'régénéré' if last else 'généré'} pour {latest['night']}"
              f" ({len(txt)} car.)")


# ---------------------------------------------------------------- main

def run(db, derived, briefing=False, recompute_all=False, now=None, log=print):
    now = int(now or time.time())
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    con.execute("PRAGMA busy_timeout=5000")
    der = sqlite3.connect(derived)
    der.execute("PRAGMA busy_timeout=5000")
    ensure_schema(der)

    axis = oc.TimeAxis(con)
    charging = oc.charging_intervals_ds(con)
    nights_raw = oc.load_nights(con, axis)
    stored = {x[0]: (x[1], x[2]) for x in der.execute("SELECT night, input_hash, version FROM sleep_scores")}
    valid = {n["night"] for n in nights_raw}
    # nuits disparues (ex. ancienne date erronée) : purge
    for night in set(stored) - valid:
        for tab in ("sleep_scores", "night_staging", "night_series", "readiness"):
            der.execute(f"DELETE FROM {tab} WHERE {'day' if tab == 'readiness' else 'night'}=?", (night,))
        log(f"nightly: nuit {night} retirée (plus de fenêtre de sommeil correspondante)")

    for n in nights_raw:
        h = night_input_hash(con, n)
        if not recompute_all and stored.get(n["night"], (None, None))[0] == h:
            continue
        row = compute_night(con, axis, n, charging)
        write_night(der, row, h)
        log(f"nightly: nuit {row['night']} — score {row['score']} ({row['total']} h, "
            f"profond {row['deep']} h, REM {row['rem']} h, eff {row['efficiency']} %, "
            f"FC basse {row['hr_min']}, HRV {row['hrv_rmssd']}, resp {row['resp_rate']}, axe {row['axis_mode']})")
    der.commit()

    nights = nights_table(der)
    update_temperature_deviation(der, nights)
    update_baselines(der, nights, now)
    activity = update_activity(con, der, axis, nights_raw, charging, recompute_all, now)
    update_readiness(der, nights, activity, now)
    der.commit()

    if briefing and nights:
        maybe_briefing(der, nights[-1], now)
        der.commit()
    if not nights_raw:
        log("nightly: aucune fenêtre de sommeil (bedtime_period) en base — rien à dériver")
    con.close()
    der.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True)
    ap.add_argument("--derived", required=True)
    ap.add_argument("--briefing", action="store_true", help="résumé LLM de la dernière nuit")
    ap.add_argument("--all", action="store_true", help="recalculer toutes les nuits")
    a = ap.parse_args()
    run(a.db, a.derived, briefing=a.briefing, recompute_all=a.all)


if __name__ == "__main__":
    main()
