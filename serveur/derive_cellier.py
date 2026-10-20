#!/usr/bin/env python3
"""Cellier — détection d'épisodes d'effort soutenu à l'anneau (onglet protégé du portail).

L'activité c'est un motif très identifiable dans le flux brut, validé sur des
épisodes réels annotés. La FC ne reste pas élevée en permanence (elle retombe
entre les vagues) : c'est la signature d'origine qui compte.

  1. Pics : 1–3+ battements courts (2–3 s) à 145–165 bpm, regroupés en
     cluster (pics à ≤ 60 min les uns des autres = une même session) ;
  2. Intensité : MET ≤ 2,5 (assis / allongé, pas de marche ni sport) ;
  3. Thermique : la température cutanée (capteur [0] du tag 70) est stable ou
     en légère montée (1–2 °C, lentement) ; une chute ≥ 2 °C (douche froide)
     ou une montée ≥ 3 °C (douche chaude) exclut l'épisode.

Niveaux : « fort » = ≥ 2 pics (signature confirmée sur des épisodes annotés)
; « possible » = 1 pic + montée cutanée ≥ 0,5 °C, ou 15 min d'élévation
soutenue de la FC (sans pic) + montée ≥ 1,0 °C. Ce sont des estimations :
quelques fausses alarmes (douche, émotion forte) sont possibles.

Le scan couvre les 35 derniers jours, recalculé à chaque passe (la table
est reconstruite de zéro, pas de déduplication à gérer).

Les épisodes en charge du portique sont exclus (le portique chauffe la peau
et fausse le signal thermique).

Usage :
    python3 derive_cellier.py --db /srv/oura/oura.db --derived /srv/oura/derived.db
"""
import argparse
import statistics
import sqlite3
from datetime import datetime, timedelta

import oura_core as oc
from oura_core import (TimeAxis, rows, jget, skin_temp,
                       T_GREEN_IBI, T_TEMP, T_ACTIVITY, HR_MIN_BPM, HR_MAX_BPM,
                       MET_BIN_S, charging_intervals_ds, in_intervals)

SCAN_DAYS = 35

# Seuils (cf. docstring)
HR_ABS = 105          # seuil absolu bas de l'élévation soutenue (bpm)
HR_BASE_MARGIN = 10   # au-dessus de la baseline mobile (bpm)
HR_PEAK = 145         # pic court (bpm) — un pic dure 2–3 s
MET_MAX = 2.5         # assis/allongé (au-dessus = exercice, exclu)
PEAK_GAP_MIN = 60     # trou max entre pics d'une même session (min)
EPISODE_PAD_MIN = 10  # marge ajoutée de chaque côté d'un cluster de pics (min)
MAX_GAP_MIN = 15      # trou toléré entre minutes d'élévation soutenue (min)
MIN_LEN_WEAK_MIN = 15 # durée minimale pour un épisode sans pic
SKIN_RISE_WEAK = 0.5  # montée cutanée (°C) pour un épisode à 1 pic
SKIN_RISE_NOLONG = 1.0  # montée cutanée pour un épisode sans pic (≥ 15 min)
SKIN_DROP_SHOWER = 2.0  # chute cutanée ≥ 2 °C = douche froide → exclu
SKIN_RISE_SHOWER = 3.0  # montée ≥ 3 °C = douche chaude → exclu (un épisode
                        # réel monte de 1–2 °C, plus lentement)
SKIN_SHOWER = 2.0       # |Δ| minute à minute ≥ 2 °C = minute hors signal
SKIN_OFF = 28.0       # peau < 28 °C = anneau enlevé ou eau froide → minute hors signal
BASE_WIN_MIN = 120    # fenêtre de baseline (min)
SKIN_BEFORE_MIN = 30  # fenêtre de référence thermique (min)

TABLE = """
CREATE TABLE cellier_episodes (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  start_unix INTEGER NOT NULL,
  end_unix INTEGER NOT NULL,
  duration_min INTEGER NOT NULL,
  hr_mean REAL,
  hr_max INTEGER,
  peak_min INTEGER NOT NULL,
  met_mean REAL,
  skin_mean REAL,
  skin_before REAL,
  skin_delta REAL,
  confidence TEXT NOT NULL,
  day TEXT NOT NULL)
"""


def _minute_bpm(con, axis, t0, t1):
    """FC par minute (tag 128) : {minute: {"mean": float, "max": int}}.

    Les fenêtres green_ibi_quality_event comportent jusqu'à ~7 battements
    contigus ; chaque battement est positionné par somme cumulée des IBI
    depuis le début de la fenêtre (temps d'horloge murale via TimeAxis)."""
    acc = {}
    s_ds, e_ds = axis.to_ds(t0 - 600), axis.to_ds(t1 + 600)
    for rt, cap, dj in rows(con, "SELECT ring_timestamp, captured_unix, decoded_json"
                             " FROM events WHERE tag=? AND ring_timestamp>=? AND ring_timestamp<?",
                            (T_GREEN_IBI, s_ds or 0, e_ds or 1 << 62)):
        j = jget(dj) or {}
        t0u = axis(rt, cap)
        if t0u is None:
            continue
        t = t0u
        for h, i in zip(j.get("hr_bpm") or [], j.get("ibi_ms") or []):
            if HR_MIN_BPM <= (h or 0) <= HR_MAX_BPM and 400 < i < 1300:
                m = int(t // 60) * 60
                if t0 <= t < t1:
                    a = acc.setdefault(m, [])
                    a.append(h)
            t += i / 1000.0
    return {m: {"mean": statistics.mean(v), "max": max(v)} for m, v in acc.items() if v}


def _minute_skin(con, axis, t0, t1):
    """Température cutanée par minute (tag 70, capteur [0]) : {minute: [°C]}."""
    acc = {}
    s_ds, e_ds = axis.to_ds(t0 - 600), axis.to_ds(t1 + 600)
    for rt, cap, dj in rows(con, "SELECT ring_timestamp, captured_unix, decoded_json"
                             " FROM events WHERE tag=? AND ring_timestamp>=? AND ring_timestamp<?",
                            (T_TEMP, s_ds or 0, e_ds or 1 << 62)):
        v = skin_temp(jget(dj))
        if v is None:
            continue
        t = axis(rt, cap)
        if t is not None and t0 <= t < t1:
            acc.setdefault(int(t // 60) * 60, []).append(v)
    return acc


def _baselines(mean_by_min, keys_sorted):
    """Baseline mobile : médiane des minutes (avec données) des BASE_WIN_MIN
    précédentes ; None si < 5 points."""
    out = {}
    for k in keys_sorted:
        lo = k - BASE_WIN_MIN * 60
        vals = [mean_by_min[m] for m in keys_sorted if lo <= m < k]
        out[k] = statistics.median(vals) if len(vals) >= 5 else None
    return out


def _skin_medians(skin, t0, t1):
    """{minute: médiane °C} + {minute: médiane de la fenêtre des SKIN_BEFORE_MIN
    min précédentes} ; la fenêtre a besoin de ≥ 3 minutes avec mesure."""
    med = {m: statistics.median(v) for m, v in skin.items() if v}
    ref = {}
    for m in med:
        lo = m - SKIN_BEFORE_MIN * 60
        vals = [med[k] for k in med if lo <= k < m]
        if len(vals) >= 3:
            ref[m] = statistics.median(vals)
    return med, ref


def detect_episodes(con, axis, t0, t1):
    """→ [épisodes] triés par date, champs de la table cellier_episodes."""
    bpm = _minute_bpm(con, axis, t0, t1)
    skin = _minute_skin(con, axis, t0, t1)
    met = oc.met_minutes(con, axis, t0, t1)
    charge = charging_intervals_ds(con)

    keys_sorted = sorted(bpm)
    mean_by_min = {m: bpm[m]["mean"] for m in keys_sorted}
    base = _baselines(mean_by_min, keys_sorted)

    def charged(m):
        return in_intervals(axis.to_ds(m), charge)

    skin_med, skin_ref = _skin_medians(skin, t0, t1)

    def anomaly(m):
        """Minute hors signal : douche (changement cutané ≥ 2 °C vs la fenêtre
        précédente) ou peau < 28 °C (anneau enlevé / eau froide)."""
        if m in skin_med:
            if skin_med[m] < SKIN_OFF:
                return True
            r = skin_ref.get(m)
            if r is not None and abs(skin_med[m] - r) >= SKIN_SHOWER:
                return True
        return False

    # --- candidats 1 : clusters de pics (≥ 145 bpm) -------------------
    peaks = [m for m in keys_sorted
             if not charged(m) and not anomaly(m) and bpm[m]["max"] >= HR_PEAK]
    clusters = []
    for m in peaks:
        if clusters and m - clusters[-1][-1] <= PEAK_GAP_MIN * 60:
            clusters[-1].append(m)
        else:
            clusters.append([m])
    cands = []
    for cl in clusters:
        cands.append((cl[0] - EPISODE_PAD_MIN * 60, cl[-1] + EPISODE_PAD_MIN * 60, "fort" if len(cl) >= 2 else "possible"))

    # --- candidats 2 : élévation soutenue sans pic ---------------------
    flagged = []
    for m in keys_sorted:
        if charged(m) or anomaly(m):
            continue
        b = base[m]
        if mean_by_min[m] >= max(HR_ABS, (b or 0) + HR_BASE_MARGIN):
            flagged.append(m)
    runs = []
    for m in flagged:
        if runs and m - runs[-1][-1] <= MAX_GAP_MIN * 60:
            if not any(anomaly(x) for x in range(runs[-1][-1] + 60, m, 60)):
                runs[-1].append(m)
                continue
        runs.append([m])
    for run in runs:
        if (run[-1] - run[0]) // 60 + 1 >= MIN_LEN_WEAK_MIN:
            cands.append((run[0], run[-1], "possible"))

    # --- fusion des candidats qui se chevauchent / se touchent ----------
    cands.sort()
    merged = []
    for s, e, c in cands:
        if merged and s <= merged[-1][1] + MAX_GAP_MIN * 60:
            merged[-1] = (merged[-1][0], max(merged[-1][1], e),
                          "fort" if c == "fort" or merged[-1][2] == "fort" else "possible")
        else:
            merged.append([s, e, c])

    out = []
    for s, e, cand_conf in merged:
        mins = [m for m in keys_sorted if s <= m <= e]
        if not mins:
            continue
        dur = (e - s) // 60 + 1
        mets = [met[m] for m in mins if m in met]
        hr_mean = statistics.mean([mean_by_min[m] for m in mins])
        hr_max = max(bpm[m]["max"] for m in mins)
        peaks_n = sum(1 for m in mins if not charged(m) and bpm[m]["max"] >= HR_PEAK)
        met_mean = statistics.mean(mets) if mets else None
        if met_mean is not None and met_mean > MET_MAX:
            continue  # exercice, exclu
        pre = [v for m, vs in skin.items() for v in vs
               if s - SKIN_BEFORE_MIN * 60 <= m < s and not charged(m)]
        skin_before = statistics.median(pre) if len(pre) >= 3 else None
        skins = [v for m in mins for v in skin.get(m, []) if not charged(m)]
        skin_mean = statistics.median(skins) if len(skins) >= 3 else None
        delta = (skin_mean - skin_before) if (skin_mean is not None and skin_before is not None) else None
        if (delta is None or delta <= -SKIN_DROP_SHOWER
                or delta >= SKIN_RISE_SHOWER):
            continue  # douche chaude/froide ou signal incomplet, exclu
        if peaks_n >= 2:
            conf = "fort"       # signature confirmée (épisodes annotés)
        elif peaks_n == 1 and delta >= SKIN_RISE_WEAK:
            conf = "possible"
        elif peaks_n == 0 and dur >= MIN_LEN_WEAK_MIN and delta >= SKIN_RISE_NOLONG:
            conf = "possible"
        else:
            continue
        out.append({
            "start_unix": s, "end_unix": e,
            "duration_min": dur,
            "hr_mean": hr_mean, "hr_max": hr_max, "peak_min": peaks_n,
            "met_mean": met_mean, "skin_mean": skin_mean,
            "skin_before": skin_before, "skin_delta": delta,
            "confidence": conf,
            "day": datetime.fromtimestamp(s).strftime("%Y-%m-%d"),
        })
    return out


def run(raw_db, derived_db, log=print):
    con = sqlite3.connect(f"file:{raw_db}?mode=ro", uri=True)
    axis = TimeAxis(con)
    t1 = int(max(r[0] for r in con.execute("SELECT MAX(captured_unix) FROM events")) or 0)
    t0 = t1 - SCAN_DAYS * 86400
    eps = detect_episodes(con, axis, t0, t1)
    con.close()

    der = sqlite3.connect(derived_db)
    der.execute("DROP TABLE IF EXISTS cellier_episodes")
    der.execute(TABLE)
    der.executemany(
        "INSERT INTO cellier_episodes (start_unix, end_unix, duration_min, hr_mean, hr_max,"
        " peak_min, met_mean, skin_mean, skin_before, skin_delta, confidence, day)"
        " VALUES (:start_unix, :end_unix, :duration_min, :hr_mean, :hr_max, :peak_min,"
        " :met_mean, :skin_mean, :skin_before, :skin_delta, :confidence, :day)",
        eps)
    der.commit()
    der.close()
    log(f"cellier: {len(eps)} épisode(s) sur {SCAN_DAYS} jours")
    return eps


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--db", required=True)
    p.add_argument("--derived", required=True)
    a = p.parse_args()
    run(a.db, a.derived)


if __name__ == "__main__":
    main()
