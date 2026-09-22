#!/usr/bin/env python3
"""derive_night.py — dérivation Phase 2 (job nightly 05:35 sur serv).

Lit /srv/oura/oura.db (brut open_oura, lecture seule) et écrit
/srv/oura/derived.db (sleep_scores, night_staging, baselines, llm_briefings).
Tout est calculé depuis les bruts, sans modèle propriétaire :

- Axe horaire : ds anneau → unix. Priorité : événements time_sync (tag 66) ;
  sinon ancrage par epoch de boot (port de open_oura tools/epoch_time.py :
  le ds maximal de chaque epoch est collé à l'heure de capture de l'événement
  qui le porte).
- Fenêtre de sommeil : bedtime_period (tag 118) — analyse embarquée de l'anneau,
  déclenchée par `oura sleep-analyze --force`.
- Staging 30 s : heuristique ouverte (mouvement sleep_acm_period + FC IBI),
  étiqueté « estimé ». Le Ring 5 n'émet pas les sleep_phase_* (confirmé en
  amont, open_oura crates/README).
- Sous-scores : anchors publiés (dmturner44/oura_sleep_score_algo, avec
  corrections des bugs de transcription visibles dans son code — voir docstring
  de subscore_*), pondération officielle 35/15/10/10/10/10/10 (confirmée par
  la calibration open_oura, R²=0.9987 — docs/algorithms/score-weights.md).
- Température nocturne + baseline EMA asymétrique : ports ecore
  (open_oura crates/oura-analysis/src/ported/{temperature,baseline}.rs).
- Briefing LLM via llama-swap (127.0.0.1:8012), modèle qwen3.5-4b.

Usage :
  python3 derive_night.py --db /srv/oura/oura.db --derived /srv/oura/derived.db [--briefing]
"""
import argparse
import bisect
import json
import sqlite3
import statistics
import sys
import time
from datetime import datetime

EPOCH_S = 30
EPOCH_DS = 300                       # 30 s en décisecondes
SLACK_DS = 6 * 3600 * 10            # port de epoch_time.py

# Tags open_oura (décimaux)
TAG_TSYNC, TAG_TEMP, TAG_STEMP, TAG_HRV, TAG_IBI, TAG_GREEN, TAG_ACM, TAG_BEDTIME = (
    66, 70, 117, 93, 96, 128, 114, 118)

MOTION_AWAKE = 2.0                  # max MAD > 2.0 → éveil (nuit réelle : p90 ≈ 0.15)
MOTION_RESTLESS = 0.5               # > 0.5 pendant le sommeil → minute agitée
HR_OUTLIER = 120                    # battements > 120 bpm ignorés (artefacts IBI documentés)
LLM_URL = "http://127.0.0.1:8012/v1/chat/completions"
LLM_MODEL = "qwen3.5-4b"
SCORE_SOURCE = "anchors dmturner44 + poids officiels (estimé, non calibré)"


def jget(s):
    if not s:
        return None
    try:
        return json.loads(s)
    except Exception:
        return None


# ---------------------------------------------------------------- horloge

def build_epochs(pairs):
    """Port de open_oura tools/epoch_time.py : epochs de boot, ancrés."""
    order = sorted((cu, ds) for ds, cu in pairs)
    epochs = []
    for cu, ds in order:
        if epochs and ds >= epochs[-1][1] - SLACK_DS:
            e = epochs[-1]
            if ds >= e[1]:
                e[1] = ds
                e[2] = cu
            e[0] = min(e[0], ds)
        else:
            epochs.append([ds, ds, cu])
    return epochs


def make_unix(epochs):
    def unix_s(ds):
        best = None
        for e in epochs:
            if e[0] - SLACK_DS <= ds <= e[1] + SLACK_DS:
                span = e[1] - e[0]
                if best is None or span < best[0]:
                    best = (span, e)
        e = best[1] if best else (epochs[-1] if epochs else None)
        if e is None:
            return None
        return e[2] - (e[1] - ds) / 10.0
    return unix_s


def time_axis(con):
    """ds → unix. Renvoie (t_of, mode) avec mode ∈ {sync, epoch, capture}."""
    anchors = []
    for rt, dj in con.execute(
            "SELECT ring_timestamp, decoded_json FROM events WHERE tag=66 ORDER BY ring_timestamp"):
        j = jget(dj)
        if j and isinstance(j.get("unix_time"), (int, float)):
            anchors.append((rt, int(j["unix_time"])))
    if anchors:
        def t_of(rt):
            off = None
            for art, au in anchors:
                if art <= rt:
                    off = au - art
                else:
                    break
            return rt + off if off is not None else None
        return t_of, "sync"
    pairs = [(r[0], r[1]) for r in con.execute("SELECT ring_timestamp, captured_unix FROM events")]
    epochs = build_epochs(pairs)
    if epochs:
        return make_unix(epochs), "epoch"
    return (lambda rt: None), "capture"


# ---------------------------------------------------------------- nuits

def load_nights(con, t_of):
    """Fenêtres de sommeil depuis bedtime_period, dédupliquées (chevauchement ≥ 50 %)."""
    rows = []
    for rt, dj in con.execute(
            "SELECT ring_timestamp, decoded_json FROM events WHERE tag=118 ORDER BY ring_timestamp"):
        j = jget(dj)
        if not j:
            continue
        s, e = j.get("bedtime_start_ds"), j.get("bedtime_end_ds")
        if not s or not e or not (3 * 36000 <= e - s <= 16 * 36000):
            continue
        su, eu = t_of(s), t_of(e)
        if su is None or eu is None or eu <= su:
            continue
        rows.append({"start_ds": s, "end_ds": e, "ref_ds": rt,
                     "start_unix": int(su), "end_unix": int(eu)})
    rows.sort(key=lambda r: r["ref_ds"])
    out = []
    for r in rows:
        merged = False
        for o in out:
            overlap = min(r["end_ds"], o["end_ds"]) - max(r["start_ds"], o["start_ds"])
            span = max(r["end_ds"] - r["start_ds"], o["end_ds"] - o["start_ds"])
            if overlap > 0 and span > 0 and overlap / span > 0.5:
                o.update(r)   # la plus récente l'emporte
                merged = True
                break
        if not merged:
            out.append(dict(r))
    for r in out:
        r["night"] = datetime.fromtimestamp(r["end_unix"]).strftime("%Y-%m-%d")
        r["bedtime"] = datetime.fromtimestamp(r["start_unix"]).strftime("%H:%M")
        r["wake_time"] = datetime.fromtimestamp(r["end_unix"]).strftime("%H:%M")
    out.sort(key=lambda r: r["night"])
    return out


# ---------------------------------------------------------------- staging

def night_signals(con, S, E):
    """Retourne (stages 30 s, motion par epoch, hr par epoch) pour [S, E)."""
    n = max(1, int(round((E - S) / EPOCH_DS)))
    motion = [0.0] * n
    for rt, dj in con.execute(
            "SELECT ring_timestamp, decoded_json FROM events"
            " WHERE tag=? AND ring_timestamp>=? AND ring_timestamp<?", (TAG_ACM, S, E)):
        j = jget(dj)
        if not j:
            continue
        vals = j.get("acm_mad") or []
        if not vals:
            continue
        i = min(n - 1, max(0, round((rt - S) / EPOCH_DS)))
        motion[i] = max(motion[i], max(vals))

    hrs = []
    for rt, dj in con.execute(
            "SELECT ring_timestamp, decoded_json FROM events"
            " WHERE tag IN (?,?) AND ring_timestamp>=? AND ring_timestamp<? ORDER BY ring_timestamp",
            (TAG_IBI, TAG_GREEN, S, E)):
        j = jget(dj)
        if not j:
            continue
        beats = [b for b in (j.get("hr_bpm") or []) if b <= HR_OUTLIER]
        if not beats:
            continue
        ibi = j.get("ibi_ms") or []
        tmid = rt + (sum(ibi) / 2000.0 if ibi else 15.0)
        hrs.append((tmid, sorted(beats)[len(beats) // 2]))
    hrs.sort()
    H = [h[0] for h in hrs]

    def hr_at(ds):
        if not hrs:
            return None
        i = bisect.bisect_left(H, ds)
        cands = []
        if i < len(hrs):
            cands.append(hrs[i])
        if i > 0:
            cands.append(hrs[i - 1])
        if not cands:
            return None
        c = min(cands, key=lambda p: abs(p[0] - ds))
        return c[1] if abs(c[0] - ds) <= 240 else None

    hr_e = [hr_at(S + EPOCH_DS / 2 + i * EPOCH_DS) for i in range(n)]
    awake = [m > MOTION_AWAKE for m in motion]
    asleep_hr = [h for i, h in enumerate(hr_e) if not awake[i] and h is not None]
    q33 = q66 = None
    if len(asleep_hr) >= 8:
        s = sorted(asleep_hr)
        q33, q66 = s[len(s) // 3], s[2 * len(s) // 3]
    stages = []
    for i in range(n):
        m = motion[i]
        if m > MOTION_AWAKE:
            st = "awake"
        else:
            h = hr_e[i]
            if h is not None and q33 is not None and m < MOTION_RESTLESS:
                st = "deep" if h <= q33 else ("rem" if h >= q66 else "light")
            else:
                st = "light"
        stages.append(st)
    return stages, motion, hr_e


def summarize_night(stages):
    """Port de la logique d'agrégation ecore (open_oura ported/sleep.rs summarize)."""
    d = {"deep": 0, "light": 0, "rem": 0, "awake": 0}
    first_sleep = None
    prev_awake = True
    wake_count = 0
    for i, st in enumerate(stages):
        d[st] += EPOCH_S
        asleep = st != "awake"
        if asleep and first_sleep is None:
            first_sleep = i
        if first_sleep is not None and st == "awake" and not prev_awake:
            wake_count += 1
        prev_awake = st == "awake"
    total = d["deep"] + d["light"] + d["rem"]
    # latence : 1re période de sommeil soutenue (20 epochs = 10 min sans éveil)
    latency = 0
    for i in range(len(stages)):
        if i + 20 <= len(stages) and all(s != "awake" for s in stages[i:i + 20]):
            latency = i * EPOCH_S
            break
    # segments de sommeil contigus
    periods, in_seg = 0, False
    for st in stages:
        if st != "awake" and not in_seg:
            periods += 1
            in_seg = True
        elif st == "awake":
            in_seg = False
    return {**d, "total": total, "latency": latency,
            "wake_count": wake_count, "periods": periods}


# ---------------------------------------------------------------- métriques

def hrv_window(con, S, E):
    vals = []
    for dj, in con.execute(
            "SELECT decoded_json FROM events WHERE tag=? AND ring_timestamp>=? AND ring_timestamp<?",
            (TAG_HRV, S, E)):
        j = jget(dj)
        if j:
            vals.extend(v for v in (j.get("rmssd_ms") or []) if v)
    if not vals:
        return None
    vals.sort()
    return vals[len(vals) // 2]


def temp_window(con, S, E):
    temps = []
    for tag in (TAG_TEMP, TAG_STEMP):
        for dj, in con.execute(
                "SELECT decoded_json FROM events WHERE tag=? AND ring_timestamp>=? AND ring_timestamp<?",
                (tag, S, E)):
            j = jget(dj)
            if j:
                temps.extend(int(round(t * 100)) for t in (j.get("temps_c") or []) if t > 5)
    return temps


def nightly_temperature_centi(samples):
    """Port ecore nightly_temperature_calculate (ported/temperature.rs)."""
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


def ashr_round(t, shift):
    adj = t + ((1 << shift) - 1) if t < 0 else t
    return adj >> shift


def ema_baseline(samples, age_days=0):
    """Port ecore baseline_update_lt_mean_and_dev (ported/baseline.rs), replié."""
    mean_x8 = dev_x8 = 0
    for i, s in enumerate(samples):
        s8 = s << 3
        delta = s8 - mean_x8
        if i > 14:
            bias = 16 if (delta != 0 and mean_x8 <= s8) else -16
            mean_x8 += ashr_round(delta + bias, 5)
        elif i >= 4:
            bias = 4 if delta > 0 else -4
            mean_x8 += ashr_round(delta + bias, 3)
        else:
            t = delta + 1 if delta > 0 else delta - 1
            mean_x8 += ashr_round(t, 1)
        absd = abs(s8 - mean_x8)
        mag, shift = (32, 6) if i > 14 else ((8, 4) if i >= 4 else (4, 3))
        bias2 = mag if (absd != dev_x8 and dev_x8 <= absd) else -mag
        dev_x8 += ashr_round((absd - dev_x8) + bias2, shift)
    return mean_x8 / 8.0, dev_x8 / 8.0


# ---------------------------------------------------------------- sous-scores
# Anchors publiés par dmturner44/oura_sleep_score_algo (constantes conservées ;
# corrections des branches où le code public oubliait la multiplication —
# voir chaque docstring).


def subscore_total_sleep(tts_s):
    """min(100, 100/9h × durée) — 100 pts à 9 h."""
    return max(0.0, min(100.0, 100.0 / (9 * 3600) * tts_s))


def subscore_rem(rem_s):
    """95 pts à 1 h 51:30 (6690 s) → 100 à 2 h 27 (8820 s)."""
    if rem_s < 6690:
        return max(0.0, 95.0 / 6690 * rem_s)
    return min(100.0, 95 + 5 / (8820 - 6690) * (rem_s - 6690))


def subscore_deep(deep_s):
    """95 pts à 1 h 33 (5580 s) → 100 à 2 h 24:30 (8670 s)."""
    if deep_s < 5580:
        return max(0.0, 95.0 / 5580 * deep_s)
    return min(100.0, 95 + 5 / (8670 - 5580) * (deep_s - 5580))


def subscore_efficiency(eff_pct):
    """37 pts à 65 % → 95 à 90 % → 100 à 95 % (linéaire par segments)."""
    if eff_pct <= 65:
        return max(0.0, 37 + (95 - 37) / (90 - 65) * (eff_pct - 65))
    if eff_pct <= 90:
        return 37 + (95 - 37) / (90 - 65) * (eff_pct - 65)
    return min(100.0, 95 + 5 / (95 - 90) * (eff_pct - 90))


def subscore_latency(lat_s):
    """59 pts à 0 s → 100 à 15 min → 22 à 44:30 → 0 à 72:30 (U inversée)."""
    if lat_s <= 900:
        return 59 + (100 - 59) / 900 * lat_s
    if lat_s <= 2670:
        return 100 - (100 - 22) / (2670 - 900) * (lat_s - 900)
    if lat_s <= 4350:
        return max(0.0, 22 - 22 / (4350 - 2670) * (lat_s - 2670))
    return 0.0


def subscore_timing(mid_sec):
    """Midpoint en s depuis 00:00 : 100 si ≤ 2 h 40 → 0 à 5 h 43 (linéaire)."""
    if mid_sec <= 9620:
        return 100.0
    if mid_sec >= 20620:
        return 0.0
    return 100.0 * (20620 - mid_sec) / (20620 - 9620)


def subscore_restfulness(awake_frac, restl_frac, r1, r2, r3, r4, periods):
    """Régression publiée (30 s de mouvement catégorisées 1..4)."""
    s = (86.019 - 27.603 * awake_frac - 2108.0 * restl_frac
         + 0.025821 * r1 + 0.020323 * r2 - 0.55056 * r3 - 0.18264 * r4
         + 2.0592 * periods)
    return max(0.0, min(100.0, s))


WEIGHTS = {
    "total_sleep": 35, "restfulness": 15, "efficiency": 10,
    "latency": 10, "deep": 10, "rem": 10, "timing": 10,
}


def compute_night(con, night, prev_temps):
    S, E = night["start_ds"], night["end_ds"]
    stages, motion, hr_e = night_signals(con, S, E)
    sum_ = summarize_night(stages)
    in_bed_s = (E - S) / 10.0

    # HR
    hrs = [h for h in hr_e if h is not None]
    hr_mean = round(sum(hrs) / len(hrs), 1) if hrs else None
    hr_min = min(hrs) if hrs else None

    # HRV
    hrv = hrv_window(con, S, E)

    # mouvement / agitations (minutes endormies avec motion > seuil)
    restless_s = 0
    restless_motions = []
    for i, st in enumerate(stages):
        if st == "awake":
            continue
        m = motion[i]
        if m > MOTION_RESTLESS:
            restless_s += EPOCH_S
            restless_motions.append(m)
    restless_motions.sort()
    n_r = len(restless_motions)
    q = max(1, n_r // 4)
    r_counts = [0.0, 0.0, 0.0, 0.0]
    for k, m in enumerate(restless_motions):
        r_counts[min(3, k // q)] += EPOCH_S / 60.0

    # température
    temps = temp_window(con, S, E)
    tnight_centi = nightly_temperature_centi(temps)
    temp_mean = round(tnight_centi / 100.0, 2) if tnight_centi is not None else None
    temp_dev = None
    if tnight_centi is not None and prev_temps:
        bmean, _ = ema_baseline(prev_temps, age_days=len(prev_temps) - 1)
        temp_dev = round(tnight_centi / 100.0 - bmean, 2)

    # timing : midpoint heure locale (0..24 h, replié en 12 h si > midi)
    a = datetime.fromtimestamp(night["start_unix"]).hour + datetime.fromtimestamp(night["start_unix"]).minute / 60
    b = datetime.fromtimestamp(night["end_unix"]).hour + datetime.fromtimestamp(night["end_unix"]).minute / 60
    if b < a:
        b += 24
    mid_h = (a + b) / 2
    if mid_h > 12:
        mid_h -= 12

    total_s = sum_["total"]
    eff = 100.0 * total_s / in_bed_s if in_bed_s else 0.0
    awake_frac = sum_["awake"] / total_s if total_s else 0.0
    restl_frac = restless_s / total_s if total_s else 0.0

    subs = {
        "total_sleep": subscore_total_sleep(total_s),
        "rem": subscore_rem(sum_["rem"]),
        "deep": subscore_deep(sum_["deep"]),
        "efficiency": subscore_efficiency(eff),
        "latency": subscore_latency(sum_["latency"]),
        "timing": subscore_timing(mid_h * 3600),
        "restfulness": subscore_restfulness(awake_frac, restl_frac,
                                            r_counts[0], r_counts[1], r_counts[2], r_counts[3],
                                            sum_["periods"]),
    }
    score = int(max(0, min(100, round(sum(WEIGHTS[k] * subs[k] for k in WEIGHTS) / 100.0))))

    row = {
        "night": night["night"],
        "score": score,
        "total": round(total_s / 3600, 2),
        "deep": round(sum_["deep"] / 3600, 2),
        "rem": round(sum_["rem"] / 3600, 2),
        "light": round(sum_["light"] / 3600, 2),
        "awake": round(sum_["awake"] / 3600, 2),
        "in_bed": round((E - S) / 10 / 3600, 2),
        "efficiency": round(eff, 1),
        "latency": round(sum_["latency"] / 60, 1),          # minutes
        "timing": round(mid_h, 2),                          # heures (midpoint)
        "restfulness": round(subs["restfulness"], 1),
        "hr_min": hr_min, "hr_mean": hr_mean,
        "hrv_rmssd": hrv,
        "temp_mean": temp_mean, "temp_dev": temp_dev,
        "movement": round(restless_s / 60, 1),             # minutes agitées
        "spo2": None,
        "bedtime": night["bedtime"], "wake_time": night["wake_time"],
        "staging_source": "heuristique",
        "subscores": json.dumps({k: round(v, 1) for k, v in subs.items()}),
        "score_source": SCORE_SOURCE,
        "ts": int(time.time()),
        "_stages": stages,
    }
    return row


# ---------------------------------------------------------------- dérivées

EXTRA_COLS = [("in_bed", "REAL"), ("light", "REAL"), ("awake", "REAL"),
              ("temp_mean", "REAL"), ("subscores", "TEXT"),
              ("score_source", "TEXT"), ("ts", "INTEGER")]


def ensure_schema(con):
    con.execute("""CREATE TABLE IF NOT EXISTS sleep_scores (
        night TEXT PRIMARY KEY, score REAL, total REAL, deep REAL, rem REAL,
        efficiency REAL, latency REAL, timing REAL, restfulness REAL,
        hr_min REAL, hr_mean REAL, hrv_rmssd REAL, temp_dev REAL,
        movement REAL, spo2 REAL, bedtime TEXT, wake_time TEXT, staging_source TEXT)""")
    con.execute("""CREATE TABLE IF NOT EXISTS night_staging (
        night TEXT, epoch INTEGER, stage TEXT, PRIMARY KEY (night, epoch))""")
    con.execute("""CREATE TABLE IF NOT EXISTS llm_briefings (
        night TEXT, model TEXT, text TEXT, ts INTEGER)""")
    con.execute("""CREATE TABLE IF NOT EXISTS baselines (
        metric TEXT PRIMARY KEY, mean REAL, sd REAL, updated_unix INTEGER)""")
    cols = {r[1] for r in con.execute("PRAGMA table_info(sleep_scores)")}
    for name, typ in EXTRA_COLS:
        if name not in cols:
            con.execute(f"ALTER TABLE sleep_scores ADD COLUMN {name} {typ}")


def write_night(con, row):
    con.execute(
        """INSERT OR REPLACE INTO sleep_scores
           (night, score, total, deep, rem, light, awake, in_bed, efficiency, latency,
            timing, restfulness, hr_min, hr_mean, hrv_rmssd, temp_mean, temp_dev,
            movement, spo2, bedtime, wake_time, staging_source, subscores, score_source, ts)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (row["night"], row["score"], row["total"], row["deep"], row["rem"],
         row["light"], row["awake"], row["in_bed"], row["efficiency"], row["latency"],
         row["timing"], row["restfulness"], row["hr_min"], row["hr_mean"], row["hrv_rmssd"],
         row["temp_mean"], row["temp_dev"], row["movement"], row["spo2"],
         row["bedtime"], row["wake_time"], row["staging_source"],
         row["subscores"], row["score_source"], row["ts"]))
    con.execute("DELETE FROM night_staging WHERE night=?", (row["night"],))
    con.executemany("INSERT INTO night_staging (night, epoch, stage) VALUES (?,?,?)",
                    [(row["night"], i, s) for i, s in enumerate(row["_stages"])])


def update_baselines(con, now):
    rows = con.execute(
        "SELECT night, hr_mean, hrv_rmssd, total, efficiency, score, temp_mean"
        " FROM sleep_scores ORDER BY night").fetchall()
    if not rows:
        return
    now = int(now)
    for metric, idx in (("hr_mean", 1), ("hrv_rmssd", 2), ("total", 3),
                        ("efficiency", 4), ("score", 5)):
        vals = [r[idx] for r in rows[:-1] if r[idx] is not None]
        if len(vals) >= 2:
            mean = sum(vals) / len(vals)
            sd = statistics.pstdev(vals)
        elif len(vals) == 1:
            mean, sd = vals[0], 0.0
        else:
            continue
        con.execute("INSERT OR REPLACE INTO baselines (metric, mean, sd, updated_unix)"
                    " VALUES (?,?,?,?)", (metric, round(mean, 3), round(sd, 3), now))
    # température : EMA asymétrique ecore replié sur les nuits précédentes
    temps = [r[6] for r in rows[:-1] if r[6] is not None]
    if temps:
        bmean, bdev = ema_baseline(temps, age_days=max(0, len(temps) - 1))
        con.execute("INSERT OR REPLACE INTO baselines (metric, mean, sd, updated_unix)"
                    " VALUES (?,?,?,?)", ("temp_nightly", round(bmean, 2), round(bdev, 2), now))


# ---------------------------------------------------------------- briefing

def briefing_context(con, night, days=30):
    rows = con.execute(
        """SELECT night, score, total, deep, rem, light, awake, in_bed, efficiency,
                  latency, timing, restfulness, hr_min, hr_mean, hrv_rmssd,
                  temp_mean, temp_dev, movement, staging_source
             FROM sleep_scores WHERE night<=? ORDER BY night DESC LIMIT ?""",
        (night, days)).fetchall()
    keys = ["night", "score", "total", "deep", "rem", "light", "awake", "in_bed",
            "efficiency", "latency", "timing", "restfulness", "hr_min", "hr_mean",
            "hrv_rmssd", "temp_mean", "temp_dev", "movement", "staging_source"]
    out = [dict(zip(keys, r)) for r in rows]
    for o in out:
        for k in list(o):
            if o[k] is None:
                del o[k]
    return json.dumps(list(reversed(out)), ensure_ascii=False)


def llm_briefing(con, night):
    """Génère le briefing de la nuit via llama-swap. None si indisponible."""
    import urllib.request
    ctx = briefing_context(con, night)
    system = (
        "Tu es l'analyste des données de sommeil de sean (anneau Oura, sans cloud). "
        "Tu reçois en contexte JSON ses dernières nuits : score et sous-scores, durée, "
        "stades, FC, RMSSD, température et déviance, latence, timing, mouvement. "
        "Écris en français, 5 à 8 lignes : analyse de la nuit écoulée, détection de "
        "tendances (latence ↑, profond ↓, température déviante, midpoint glissant…), "
        "et 1 suggestion actionnable. INTERDICTION d'inventer des chiffres absents du "
        "contexte : si une donnée manque, dis-le. Précise que les scores sont des "
        "estimations (staging heuristique, non calibrés)."
    )
    payload = {
        "model": LLM_MODEL,
        "messages": [
            {"role": "system", "content": system + "\nContexte (nuits) :\n" + ctx},
            {"role": "user", "content": f"Rédige le briefing de la nuit du {night}."},
        ],
        "temperature": 0.4,
        "max_tokens": 800,
        "chat_template_kwargs": {"enable_thinking": False},
    }
    req = urllib.request.Request(LLM_URL, data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=180) as r:
            text = (json.load(r)["choices"][0]["message"].get("content") or "").strip()
        if not text:
            return None
        con.execute("INSERT INTO llm_briefings (night, model, text, ts) VALUES (?,?,?,?)",
                    (night, LLM_MODEL, text, int(time.time())))
        return text
    except Exception as e:  # noqa: BLE001
        print(f"nightly: briefing LLM indisponible : {e}", file=sys.stderr)
        return None


# ---------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True)
    ap.add_argument("--derived", required=True)
    ap.add_argument("--briefing", action="store_true",
                    help="générer le briefing LLM de la nuit la plus récente nouvelle")
    args = ap.parse_args()

    con = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    con.execute("PRAGMA busy_timeout=2000")
    der = sqlite3.connect(args.derived)
    der.execute("PRAGMA busy_timeout=5000")
    ensure_schema(der)

    t_of, mode = time_axis(con)
    nights = load_nights(con, t_of)
    known = {r[0] for r in der.execute("SELECT night FROM sleep_scores")}
    new_nights = [n for n in nights if n["night"] not in known]

    # températures déjà consolidées (nuits précédentes, pour la baseline)
    prev_temps = []
    for r in der.execute("SELECT temp_mean FROM sleep_scores ORDER BY night"):
        if r[0] is not None:
            prev_temps.append(int(round(r[0] * 100)))

    rows = []
    for n in nights:
        row = compute_night(con, n, prev_temps)
        write_night(der, row)
        prev_temps.append(int(round(row["temp_mean"] * 100))) if row["temp_mean"] else None
        rows.append(row)
        print(f"nightly: nuit {row['night']} — score {row['score']}/100 "
              f"({row['total']} h, eff {row['efficiency']} %, latence {row['latency']} min, "
              f"staging {row['staging_source']}, axe horaire: {mode})")

    update_baselines(der, time.time())
    der.commit()

    if args.briefing:
        latest = max(rows, key=lambda r: r["night"]) if rows else None
        if latest:
            have = der.execute("SELECT 1 FROM llm_briefings WHERE night=? LIMIT 1",
                               (latest["night"],)).fetchone()
            if not have:
                txt = llm_briefing(der, latest["night"])
                if txt:
                    der.commit()
                    print(f"nightly: briefing LLM généré pour {latest['night']} ({len(txt)} car.)")
    con.close()
    der.close()
    if not nights:
        print("nightly: aucune fenêtre de sommeil (bedtime_period) en base — rien à dériver")


if __name__ == "__main__":
    main()
