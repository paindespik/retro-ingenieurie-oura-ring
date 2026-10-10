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
  baselines       lignes de base personnelles (nuits SAINES antérieures à la
                  dernière ; chaque nuit garde aussi les siennes : ref_json)
  readiness       contributeurs de récupération + signes de tension, par jour
  activity_daily  activité par journée de 04:00 à 03:59 (MET, inactivité, pas estimés NON validés,
                  dépense totale estimée, FC de jour, couverture des données)
  rests           repos détectés par l'anneau hors des nuits (sieste probable / repos)
  llm_briefings   résumé LLM de la dernière nuit (régénéré si ses entrées changent)

Définitions alignées sur la documentation Oura quand elle existe :
- FC la plus basse = minimum des moyennes glissantes 10 min (bins 5 min de
  l'anneau) pendant le sommeil ; HRV = moyenne des RMSSD 5 min (+ max) ;
- Recovery Index = sommeil restant après la STABILISATION de la FC : premier
  instant où la moyenne sur 10 min revient à moins de 3 bpm de la FC la plus
  basse (Oura, « Readiness Score » ; optimal ≥ 6 h). Le minimum strict est
  souvent un plateau atteint tard : l'indice basculait pour 0,5 bpm ;
- température de la nuit = médiane des échantillons cutanés (tag 117) entre
  l'endormissement et la fin du sommeil (l'estimateur ecore, minimum des
  maxima de fenêtres de 30 échantillons, reste en valeur secondaire
  temp_ecore : sur des échantillons à 30 s, une seule fenêtre froide le fixe,
  σ entre nuits saines 0,50 °C contre 0,28 °C pour la médiane) ;
- écart de température = nuit − médiane des nuits précédentes (≤ 30),
  « provisoire » avant 14 nuits (Oura : ~2 semaines de calibrage) ;
- références personnelles : nuits antérieures SAINES seulement (sont exclues
  les nuits qui suivent un jour noté « maladie » dans le journal et celles
  qui présentent des signes de tension marqués), sinon une infection gonfle
  la moyenne et l'écart-type et masque les écarts des nuits suivantes ;
- une nuit reste « provisoire » tant qu'aucun signe d'éveil n'a été observé
  après la fin de sa fenêtre (l'anneau la prolonge au fil de la nuit) : pas de
  résumé du matin avant sa clôture.

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

VERSION = 5                      # incrémenter → recalcul de toutes les nuits
LLM_URL = os.environ.get("OURA_LLM_URL", "http://127.0.0.1:8012/v1/chat/completions")
# Résumé du matin : le 27B résident de la RTX 3090 (préchargé par llama-swap,
# jamais déchargé) quand il est disponible, sinon le 4B de la P40. On ne
# demande JAMAIS le 27B si un autre modèle occupe la 3090 : llama-swap le
# déchargerait (un seul modèle à la fois sur cette carte).
LLM_MODEL = os.environ.get("OURA_LLM_MODEL", "qwen3.8-27b")
LLM_MODEL_FALLBACK = os.environ.get("OURA_LLM_MODEL_FALLBACK", "qwen3.5-4b")
# « medium » : ~80 s par résumé, nettement plus précis que « low » (~40 s) sur
# les nuits réelles (valeurs et sous-scores cités, causalité prudente).
LLM_REASONING = os.environ.get("OURA_LLM_REASONING", "medium")   # low | medium | xhigh
SCORE_SOURCE = ("sous-scores : anchors publiés (dmturner44/oura_sleep_score_algo) + "
                "restfulness locale ; poids Oura 35/15/10/10/10/10/10 — estimation")
# Besoin de sommeil NON personnalisé : 7 h 30, choisi par l'utilisateur le 10/10/2026
# en attendant une estimation personnelle (Oura : 90 jours d'historique, nuits
# atypiques filtrées), prévue à la passe de conformité à ~30 nuits.
SLEEP_NEED_H = 7.5
# Dette de sommeil (Oura, support « Sleep Debt ») : 14 derniers jours, nuits
# récentes pondérées davantage, au moins 5 nuits ; aucune / faible < 2 h /
# modérée 2–5 h / élevée > 5 h.
SLEEP_DEBT_DAYS = 14
SLEEP_DEBT_MIN_NIGHTS = 5
TEMP_BASE_NIGHTS = 30
REF_NIGHTS = 60
BASE_RELIABLE_N = 14
SICK_TAG = "maladie"
# Équilibre HRV (Oura : 14 derniers jours pondérés vs ~3 mois) : tant que la
# fenêtre longue n'est guère plus longue que la courte, le rapport vaut ~1 et le
# contributeur sature à 100 → « calibrage », hors du score, avant 60 nuits.
HRV_BALANCE_MIN_NIGHTS = 60

# Clôture de la nuit. L'anneau réémet la fenêtre « bedtime » en cours au fil de
# la nuit (fin = heure d'émission), parfois après 4 à 5 h sans révision : une
# nuit n'est close qu'une fois un signe d'éveil observé APRÈS la fin de sa
# fenêtre. Rejeu aux heures du timer sur 19 nuits réelles : 0 nuit tronquée,
# clôture ~40 min après le réveil (médiane), contre 12 nuits tronquées de 1 h 30
# à 4 h 15 avec l'ancienne règle « fin + 45 min ».
CLOSE_HR_MEASURES = 3            # mesures de FC de jour (tag 128) après fin + 5 min
CLOSE_MET_MIN = 10               # minutes à ≥ 1,5 MET après la fin
CLOSE_FALLBACK_S = 6 * 3600      # sans signe d'éveil (téléphone muet…) : close à fin + 6 h


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
    raw_beats = oc.load_beats(con, S, E)
    beats = oc.clean_beats(raw_beats)
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
    bins_ok = 100.0 * len(hrs) / len(sleep_bins) if sleep_bins else None
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
    # stabilisation : 1er instant où la moyenne sur 10 min ≤ FC la plus basse + 3 bpm
    hr_stable_at = hr_low_at
    if hr_low is not None:
        for (c0, h0), (c1, h1) in zip(hrs, hrs[1:]):
            if c1 - c0 <= 3300 and (h0 + h1) / 2 <= hr_low + 3:
                hr_stable_at = (c0 + c1) / 2
                break
    recovery_h = None
    if hr_stable_at is not None:
        k = int((hr_stable_at - S) // staging.EPOCH_DS)
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
    temp_ecore = t_centi / 100.0 if t_centi is not None else None
    asleep_t = [t for rt, t in temps if onset_ds <= rt < end_ds]
    temp_mean = (oc.median(asleep_t) if len(asleep_t) >= 60 else
                 oc.median([t for _, t in temps]) if len(temps) >= 60 else temp_ecore)

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
        "hr_stable_at": int(axis(hr_stable_at)) if hr_stable_at else None,
        "resp_rate": r(resp_rate, 1),
        "resp_n": len(resp),
        "beats_kept_pct": r(100.0 * len(beats) / len(raw_beats), 1) if raw_beats else None,
        "lead_min": feats["lead"] * staging.EPOCH_DS / 600,
        "hr_bins_pct": r(bins_ok, 1),
        "temp_mean": r(temp_mean, 2),
        "temp_ecore": r(temp_ecore, 2),
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


def night_closed(con, axis, night, now):
    """La nuit est-elle terminée ? → (bool, motif). Signe d'éveil APRÈS la fin
    de la fenêtre, parmi les données captées à `now` : reprise des mesures de
    FC de jour (tag 128, suspendues pendant le sommeil) ou de l'activité
    (≥ 10 min à ≥ 1,5 MET). Sans aucun signe (téléphone muet), repli à fin + 6 h."""
    end = night["end_unix"]
    hr = 0
    for rt, cap in oc.rows(con, "SELECT ring_timestamp, captured_unix FROM events WHERE tag=?"
                                " AND captured_unix>=? AND captured_unix<=?",
                           (oc.T_GREEN_IBI, int(end) - 3600, int(now))):
        t = axis(rt, cap)
        if t is not None and t > end + 300:
            hr += 1
    if hr >= CLOSE_HR_MEASURES:
        return True, "FC de jour reprise"
    met = 0
    for rt, cap, dj in oc.rows(con, "SELECT ring_timestamp, captured_unix, decoded_json FROM events"
                                    " WHERE tag=? AND captured_unix>=? AND captured_unix<=?",
                               (oc.T_ACTIVITY, int(end) - 3600, int(now))):
        vals = (oc.jget(dj) or {}).get("met") or []
        t_end = axis(rt, cap)
        if t_end is None:
            continue
        met += sum(1 for k, v in enumerate(vals)
                   if v is not None and v >= 1.5 and t_end - (len(vals) - k) * oc.MET_BIN_S >= end)
    if met >= CLOSE_MET_MIN:
        return True, "activité reprise"
    if now - end >= CLOSE_FALLBACK_S:
        return True, "délai écoulé sans signe d'éveil"
    return False, None


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
    ("provisional", "INTEGER"), ("ref_json", "TEXT"), ("ref_excluded", "TEXT"),
    ("beats_kept_pct", "REAL"), ("lead_min", "REAL"), ("hr_bins_pct", "REAL"),
    ("hr_stable_at", "INTEGER"), ("temp_ecore", "REAL"), ("brief_hash", "TEXT"),
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
        CREATE TABLE IF NOT EXISTS rests (
            start_unix INTEGER PRIMARY KEY, end_unix INTEGER, day TEXT, minutes REAL,
            kind TEXT, hr_median REAL, move_pct REAL, ts INTEGER);
        CREATE TABLE IF NOT EXISTS user_profile (
            id INTEGER PRIMARY KEY CHECK (id=1), age_years INTEGER, height_cm INTEGER,
            weight_kg REAL, sex TEXT, updated_unix INTEGER);
    """)
    for tab, col, typ in (("llm_briefings", "input_hash", "TEXT"), ("llm_briefings", "checked", "INTEGER"),
                          ("baselines", "median", "REAL"),
                          ("baselines", "n", "INTEGER"),
                          ("readiness", "sleep_debt_h", "REAL"), ("readiness", "sleep_debt_n", "INTEGER"),
                          ("readiness", "sleep_need_h", "REAL"),
                          *(("activity_daily", c, t) for c, t in ACTIVITY_EXTRA_COLS)):
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


BASELINE_METRICS = ("hr_min", "hr_mean", "hrv_rmssd", "resp_rate", "temp_mean", "total",
                    "efficiency", "score", "deep", "rem", "waso", "recovery_index")


def base_stats(vals):
    vals = [v for v in vals if v is not None]
    if not vals:
        return None
    return {"mean": statistics.fmean(vals), "sd": statistics.pstdev(vals) if len(vals) > 1 else 0.0,
            "median": statistics.median(vals), "n": len(vals)}


def sick_nights(der):
    """Nuits qui suivent un jour noté « maladie » dans le journal : le tag du
    jour J décrit la journée, la nuit concernée est celle qui se termine le
    matin de J+1 (même convention que la page Sommeil)."""
    out = set()
    for (day,) in der.execute("SELECT DISTINCT day FROM tags WHERE kind=?", (SICK_TAG,)):
        try:
            out.add((datetime.strptime(day, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d"))
        except (TypeError, ValueError):
            continue
    return out


def ref_stats(ref):
    """Statistiques de référence par métrique (format de la table baselines)."""
    out = {}
    for m in BASELINE_METRICS:
        b = base_stats([p.get(m) for p in ref])
        if b:
            out[m] = {"mean": round(b["mean"], 3), "sd": round(b["sd"], 3),
                      "median": round(b["median"], 3), "n": b["n"]}
    return out


def update_references(der, nights):
    """Passe chronologique, causale : pour chaque nuit, les références sont les
    nuits SAINES strictement antérieures (≤ 60 ; ≤ 30 pour la température).

    Une nuit est exclue des références des nuits suivantes si elle suit un jour
    noté « maladie » dans le journal, ou si elle présente elle-même des signes
    de tension marqués (calculés contre ses propres références) : une infection
    non notée ne doit pas non plus gonfler la moyenne et l'écart-type.

    Écrit pour chaque nuit l'écart de température, ses références figées
    (ref_json, affichées sur sa page même des semaines plus tard) et le motif
    d'exclusion ; renseigne n["_ref"] et n["_tension"] pour la récupération."""
    sick = sick_nights(der)
    clean = []
    for i, n in enumerate(nights):
        ref = clean[-REF_NIGHTS:]
        prev_t = [p["temp_mean"] for p in clean[-TEMP_BASE_NIGHTS:] if p["temp_mean"] is not None]
        dev, status = None, "calibrage"
        if n["temp_mean"] is not None and len(prev_t) >= 3:
            dev = round(n["temp_mean"] - statistics.median(prev_t), 2)
            status = "fiable" if len(prev_t) >= BASE_RELIABLE_N else "provisoire"
        n["temp_dev"], n["temp_n"], n["temp_status"] = dev, len(prev_t), status
        n["_ref"] = ref
        n["_tension"] = tension_for(i, nights, ref)
        reason = ("maladie (journal)" if n["night"] in sick else
                  "signes de tension marqués" if n["_tension"]["level"] == "marqués" else None)
        n["ref_json"] = json.dumps(ref_stats(ref))
        n["ref_excluded"] = reason
        der.execute("UPDATE sleep_scores SET temp_dev=?, temp_n=?, temp_status=?, ref_json=?,"
                    " ref_excluded=? WHERE night=?",
                    (dev, len(prev_t), status, n["ref_json"], reason, n["night"]))
        if reason is None:
            clean.append(n)


def update_baselines(der, nights, now):
    """Lignes de base affichées : références de la dernière nuit, c.-à-d. les
    nuits saines qui la précèdent (≤ 60)."""
    der.execute("DELETE FROM baselines")
    if not nights:
        return
    last = nights[-1]
    prev = last["_ref"] if "_ref" in last else nights[:-1][-REF_NIGHTS:]
    for m in BASELINE_METRICS:
        b = base_stats([p.get(m) for p in prev])
        if b:
            der.execute("INSERT INTO baselines (metric, mean, sd, median, n, updated_unix)"
                        " VALUES (?,?,?,?,?,?)",
                        (m, round(b["mean"], 3), round(b["sd"], 3), round(b["median"], 3), b["n"], now))


# ---------------------------------------------------------------- récupération

def sleep_debt(i, nights):
    """Dette de sommeil (Oura, « Sleep Debt ») : manque de sommeil des 14 derniers
    jours par rapport au besoin, les nuits récentes pesant davantage (poids 1,5
    pour la nuit du jour à 0,5 il y a 13 jours) ; un surplus rembourse la dette,
    qui ne descend pas sous 0. Nuits non enregistrées ignorées. → (heures | None,
    nombre de nuits) ; None avant 5 nuits sur la fenêtre."""
    d_i = datetime.strptime(nights[i]["night"], "%Y-%m-%d")
    acc, k = 0.0, 0
    for p in nights[max(0, i - 3 * SLEEP_DEBT_DAYS):i + 1]:
        age = (d_i - datetime.strptime(p["night"], "%Y-%m-%d")).days
        if 0 <= age < SLEEP_DEBT_DAYS and p.get("total") is not None:
            acc += (1.5 - age / (SLEEP_DEBT_DAYS - 1)) * (SLEEP_NEED_H - p["total"])
            k += 1
    if k < SLEEP_DEBT_MIN_NIGHTS:
        return None, k
    return round(max(0.0, acc), 2), k


def readiness_for(i, nights, activity, ref=None):
    """Contributeurs de type Readiness (Oura) pour le jour de la nuit i.
    Chaque contributeur 0–100, None si données insuffisantes."""
    n = nights[i]
    prev = nights[max(0, i - REF_NIGHTS):i] if ref is None else ref
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
        mature = len(long_) >= HRV_BALANCE_MIN_NIGHTS
        c["hrv_balance"] = {"score": oc.lerp_score(ratio, [(0.6, 15), (0.75, 45), (0.9, 75),
                                                           (1.0, 95), (1.05, 100)]) if mature else None,
                            "value": round(recent, 1), "baseline": round(statistics.fmean(long_), 1),
                            "delta": round((ratio - 1) * 100, 1), "nights": len(long_),
                            "status": None if mature else "calibrage"}

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


def tension_for(i, nights, ref=None):
    """Signes de tension (analogue non diagnostique du Symptom Radar Oura) :
    écarts simultanés de la FC la plus basse, de la FC moyenne, du HRV, de la
    température et de la respiration par rapport aux nuits antérieures."""
    n = nights[i]
    prev = nights[max(0, i - REF_NIGHTS):i] if ref is None else ref
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
        score, contrib, nb = readiness_for(i, nights, activity, n.get("_ref"))
        tension = n.get("_tension") or tension_for(i, nights, n.get("_ref"))
        debt, debt_n = sleep_debt(i, nights)
        der.execute("INSERT OR REPLACE INTO readiness (day, score, contributors, tension, n_base,"
                    " provisional, ts, sleep_debt_h, sleep_debt_n, sleep_need_h) VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (n["night"], score, json.dumps(contrib, ensure_ascii=False),
                     json.dumps(tension, ensure_ascii=False), nb, int(nb < BASE_RELIABLE_N), now,
                     debt, debt_n, SLEEP_NEED_H))


# ---------------------------------------------------------------- activité

# Colonnes ajoutées à activity_daily (lot B) : pas (estimation non validée),
# dépense, FC de jour, qualité des données du jour.
ACTIVITY_EXTRA_COLS = (("awake_min", "INTEGER"), ("in_bed_min", "INTEGER"), ("nonworn_min", "INTEGER"),
                       ("rest_awake_min", "INTEGER"),
                       ("steps", "INTEGER"), ("bmr_kcal", "REAL"), ("total_kcal", "REAL"),
                       ("hr_day_n", "INTEGER"), ("hr_day_rest", "REAL"), ("hr_day_mean", "REAL"),
                       ("hr_day_max", "REAL"), ("met_cover_pct", "REAL"), ("charge_min", "INTEGER"),
                       ("rest_min", "INTEGER"))
ACTIVITY_COLS = ("worn_min", "sleep_min", "inactive_min", "low_min", "medium_min", "high_min",
                 "met_min_mh", "sedentary_bouts", "active_kcal", "easy_day", "partial",
                 *(c for c, _ in ACTIVITY_EXTRA_COLS))


def profile_weight(der, con):
    row = der.execute("SELECT weight_kg FROM user_profile WHERE id=1").fetchone()
    if row and row[0]:
        return float(row[0]), "profil"
    ui = con.execute("SELECT decoded_json FROM events WHERE tag=? ORDER BY ring_timestamp DESC LIMIT 1",
                     (oc.T_USER_INFO,)).fetchone()
    w = (oc.jget(ui[0]) or {}).get("weight_kg") if ui else None
    return (float(w), "anneau") if w else (70.0, "défaut")


def profile_bmr(der):
    """Métabolisme de base du profil saisi dans le portail (None s'il est incomplet)."""
    row = der.execute("SELECT age_years, height_cm, weight_kg, sex FROM user_profile WHERE id=1").fetchone()
    return oc.bmr_kcal(*row) if row else None


def pctl(vals, p):
    """Percentile p (0–100) par interpolation linéaire ; vals triées."""
    if not vals:
        return None
    k = (len(vals) - 1) * p / 100.0
    i = int(k)
    return vals[i] + (vals[min(i + 1, len(vals) - 1)] - vals[i]) * (k - i)


def overlap_min(windows, t0, t1):
    return sum(max(0.0, min(b, t1) - max(a, t0)) for a, b in windows) / 60.0


SEDENTARY_BOUT_MIN = 60          # Oura (doc 2021) : périodes inactives ≥ 60 min
SEDENTARY_GAP_MIN = 2            # mouvement bref toléré (s'agiter sur sa chaise)


def sedentary_bouts(grid):
    """Périodes assises ≥ 60 min : minutes d'éveil à < 1,5 MET (ou sans mesure
    d'intensité ni mouvement). Jusqu'à 2 minutes d'éveil plus actives ne coupent
    pas la période (sans cette tolérance, une seule minute à 1,5 MET la coupait :
    0 période sur 13 jours sur 18) ; tout autre état (lit, repos, charge, retrait,
    non porté) ou 3 minutes actives la terminent."""
    bouts, run, gap = 0, 0, 0
    for m in sorted(grid):
        g = grid[m]
        awake = g["state"] == "éveil"
        sed = awake and ((g["met"] is not None and g["met"] < oc.MET_LIGHT)
                         or (g["met"] is None and not g["motion"]))
        if sed:
            run, gap = run + gap + 1 if run else 1, 0
        elif awake and run and gap < SEDENTARY_GAP_MIN:
            gap += 1
        else:
            bouts += run >= SEDENTARY_BOUT_MIN
            run, gap = 0, 0
    return bouts + (run >= SEDENTARY_BOUT_MIN)


def compute_activity_day(con, axis, day, nights, rests, charging_ds, weight, now=None, bmr=None,
                         removals=()):
    """Journée d'activité de 04:00 à 03:59 (cf. oc.activity_grid pour les états
    de chaque minute). nights : [(début, fin, sommeil_h)] ; rests : [(début, fin)]."""
    now = time.time() if now is None else now
    t0, t1 = oc.activity_day_bounds(day)
    end = min(t1, now)
    charges = [(axis(a), axis(b)) for a, b in charging_ds]
    grid = oc.activity_grid(con, axis, t0, end, [(a, b) for a, b, _ in nights], rests, charges,
                            removals, charging_ds) if end > t0 else {}
    awake = [g for g in grid.values() if g["state"] == "éveil"]
    measured = [g["met"] for g in awake if g["met"] is not None]
    rest_awake = sum(1 for v in measured if v < oc.MET_REST)
    inactive = sum(1 for v in measured if oc.MET_REST <= v < oc.MET_LIGHT)
    low = sum(1 for v in measured if oc.MET_LIGHT <= v < oc.MET_MODERATE)
    med = sum(1 for v in measured if oc.MET_MODERATE <= v < oc.MET_VIGOROUS)
    high = sum(1 for v in measured if v >= oc.MET_VIGOROUS)
    met_mh = sum(v for v in measured if v >= oc.MET_MODERATE)
    kcal = sum((v - 1) * 3.5 * weight / 200 for v in measured if v >= oc.MET_LIGHT)
    states = {}
    for g in grid.values():
        states[g["state"]] = states.get(g["state"], 0) + 1
    # sommeil mesuré au prorata de la part de chaque nuit tombant dans la journée
    sleep_min = sum(h * 60 * overlap_min([(a, b)], t0, end) * 60 / (b - a)
                    for a, b, h in nights if h and b > a)

    # FC de jour (tag 128) sur les minutes d'éveil : moyenne, « FC de repos de
    # jour » = 10e percentile des minutes calmes (MET connu < 1,5), maximum =
    # 99e percentile (une minute isolée peut être un artefact)
    hr = {m: v for m, v in oc.day_hr_minutes(con, axis, t0, end).items()
          if grid.get(m, {}).get("state") == "éveil"} if grid else {}
    vals = sorted(hr.values())
    calm = sorted(v for m, v in hr.items() if grid[m]["met"] is not None and grid[m]["met"] < oc.MET_LIGHT)

    frac = max(0.0, min(1.0, (end - t0) / 86400))
    bmr_day = bmr * frac if bmr else None
    return {"day": day,
            "worn_min": sum(n for s, n in states.items() if s in ("éveil", "lit", "repos")),
            "awake_min": len(awake), "in_bed_min": states.get("lit", 0), "sleep_min": round(sleep_min),
            "rest_min": states.get("repos", 0), "nonworn_min": states.get("non porté", 0) + states.get("retiré", 0),
            "charge_min": states.get("charge", 0),
            "rest_awake_min": rest_awake, "inactive_min": inactive, "low_min": low, "medium_min": med,
            "high_min": high, "met_min_mh": round(met_mh, 1), "sedentary_bouts": sedentary_bouts(grid),
            "active_kcal": round(kcal), "easy_day": int(high <= 15 and med + high <= 85),
            "partial": int(len(measured) < 600 or t1 > now),
            "steps": int(round(sum(g["steps"] for g in grid.values()))),
            "bmr_kcal": round(bmr_day) if bmr_day else None,
            "total_kcal": round(bmr_day) + round(kcal) if bmr_day else None,
            "hr_day_n": len(vals),
            "hr_day_mean": r(statistics.fmean(vals), 1) if len(vals) >= 30 else None,
            "hr_day_rest": r(pctl(calm, 10), 1) if len(calm) >= 20 else None,
            "hr_day_max": r(pctl(vals, 99), 1) if len(vals) >= 30 else None,
            "met_cover_pct": r(100.0 * len(measured) / len(awake), 1) if awake else None}


# Barèmes sourcés (Oura, « Activity Contributors » et doc API 2021) :
# rester actif : 5–8 h favorables, > 95 sous 7 h, « attention » au-delà de 12 h ;
# bouger chaque heure : périodes assises ≥ 60 min, > 95 avec une seule ;
# volume : 95 dès 750 MET-min par semaine, 100 à 2 000.
STAY_ACTIVE_PTS = [(5, 100), (7, 95), (8, 85), (12, 55), (16, 20)]
MOVE_HOURLY_PTS = [(0, 100), (1, 95), (3, 65), (6, 30)]
TRAINING_VOLUME_PTS = [(0, 20), (750, 95), (2000, 100)]


def activity_contributors(day, activity):
    """Contributeurs du score d'activité ; score = MOYENNE SIMPLE (pondération
    locale : Oura ne publie plus ses poids), à partir de 5 contributeurs."""
    a = activity[day]
    c = {"stay_active": {"score": oc.lerp_score(a["inactive_min"] / 60, STAY_ACTIVE_PTS),
                         "value": a["inactive_min"]},
         "move_every_hour": {"score": oc.lerp_score(a["sedentary_bouts"], MOVE_HOURLY_PTS),
                             "value": a["sedentary_bouts"]}}
    d0 = datetime.strptime(day, "%Y-%m-%d")
    week = [activity.get((d0 - timedelta(days=k)).strftime("%Y-%m-%d")) for k in range(7)]
    week = [w for w in week if w and (w.get("awake_min") or w["worn_min"]) >= 600]
    if len(week) >= 3:
        freq = sum(1 for w in week if w["met_min_mh"] >= 100)
        vol = sum(w["met_min_mh"] for w in week) * 7 / len(week)
        easy = sum(w["easy_day"] for w in week)
        c["training_frequency"] = {"score": oc.lerp_score(freq, [(0, 20), (1, 45), (2, 70), (3, 95), (4, 100)]),
                                   "value": freq, "days": len(week)}
        c["training_volume"] = {"score": oc.lerp_score(vol, TRAINING_VOLUME_PTS),
                                "value": round(vol), "days": len(week)}
        c["recovery_time"] = {"score": oc.lerp_score(easy, [(0, 30), (1, 85), (2, 100)]),
                              "value": easy, "days": len(week)}
    for k in c:
        c[k]["score"] = r(c[k]["score"], 0)
        c[k]["rating"] = oc.rating(c[k]["score"])
    sc = [x["score"] for x in c.values()]
    return (round(statistics.fmean(sc)) if len(sc) >= 5 else None), c


def update_activity(con, der, axis, nights, charging, recompute_all, now, rests=()):
    """nights : lignes de sleep_scores (fenêtres et sommeil mesuré)."""
    known = {x[0]: x[1] for x in der.execute("SELECT day, partial FROM activity_daily")}
    first = con.execute("SELECT MIN(captured_unix) FROM events WHERE tag=?", (oc.T_ACTIVITY,)).fetchone()[0]
    if not first:
        return {}
    d = datetime.strptime(oc.activity_day(first), "%Y-%m-%d").date()
    today = datetime.strptime(oc.activity_day(now), "%Y-%m-%d").date()
    weight, _ = profile_weight(der, con)
    bmr = profile_bmr(der)
    night_w = [(n["start_unix"], n["stop_unix"], n.get("total")) for n in nights if n.get("start_unix")]
    rest_w = [(p["start_unix"], p["end_unix"]) for p in rests]
    removals = oc.wear_removals(con, axis)
    cols = ("day", *ACTIVITY_COLS, "ts")
    while d <= today:
        day = d.strftime("%Y-%m-%d")
        if recompute_all or day not in known or known[day] or (today - d).days <= 1:
            a = compute_activity_day(con, axis, day, night_w, rest_w, charging, weight, now, bmr, removals)
            a["ts"] = now
            der.execute("INSERT OR REPLACE INTO activity_daily (%s) VALUES (%s)"
                        % (", ".join(cols), ", ".join("?" * len(cols))), [a.get(c) for c in cols])
        d += timedelta(days=1)
    der.row_factory = sqlite3.Row
    act = {x["day"]: dict(x) for x in der.execute("SELECT * FROM activity_daily")}
    der.row_factory = None
    for day in act:
        score, contrib = activity_contributors(day, act)
        if act[day]["partial"]:
            # journée en cours (ou trop peu de minutes d'éveil mesurées) : les
            # contributeurs restent visibles, mais un score sur 3 h de données
            # (inactivité faible → « Optimal ») serait trompeur
            score = None
        der.execute("UPDATE activity_daily SET score=?, contributors=? WHERE day=?",
                    (score, json.dumps(contrib, ensure_ascii=False), day))
        act[day]["score"], act[day]["contributors"] = score, contrib
    return act


# ---------------------------------------------------------------- repos et siestes

NAP_MIN_MIN = 15                 # Oura : siestes de 15 min à 3 h
NAP_MAX_MOVE_PCT = 20            # part maximale d'epochs à mouvement franc
NAP_HR_MARGIN = 5                # FC médiane ≤ FC moyenne de sommeil habituelle + 5 bpm


def update_rests(con, der, nights_raw, nights, rests, now):
    """Repos détectés par l'anneau hors des nuits (bedtime_period < 3 h).
    « sieste probable » si ≥ 15 min, peu de mouvement et une FC proche de celle
    du sommeil (référence : FC moyenne des nuits saines) ; « début de nuit » si
    la période se termine moins de 30 min avant une nuit ; sinon « repos »
    (allongé, éveillé : FC diurne). Estimation : pas de staging sur ces périodes,
    trop courtes pour normaliser les caractéristiques."""
    der.execute("DELETE FROM rests")
    ref = json.loads((nights[-1].get("ref_json") if nights else None) or "{}").get("hr_mean")
    for p in rests:
        s, e = p["start_ds"], p["end_ds"]
        hr = [60000.0 / x for _, x in oc.clean_beats(oc.load_beats(con, s, e))]
        mot = [max((oc.jget(dj) or {}).get("acm_mad") or [0.0])
               for dj, in con.execute("SELECT decoded_json FROM events WHERE tag=? AND ring_timestamp>=?"
                                      " AND ring_timestamp<?", (oc.T_ACM, s, e))]
        move = 100.0 * sum(1 for m in mot if m > staging.MOTION_AWAKE) / len(mot) if mot else None
        hr_med = statistics.median(hr) if len(hr) >= 60 else None
        minutes = (p["end_unix"] - p["start_unix"]) / 60
        if any(0 <= n["start_unix"] - p["end_unix"] <= 1800 for n in nights_raw):
            kind = "début de nuit"
        elif (minutes >= NAP_MIN_MIN and hr_med is not None and ref and move is not None
              and move <= NAP_MAX_MOVE_PCT and hr_med <= ref["mean"] + NAP_HR_MARGIN):
            kind = "sieste probable"
        else:
            kind = "repos"
        der.execute("INSERT OR REPLACE INTO rests (start_unix, end_unix, day, minutes, kind, hr_median,"
                    " move_pct, ts) VALUES (?,?,?,?,?,?,?,?)",
                    (int(p["start_unix"]), int(p["end_unix"]),
                     datetime.fromtimestamp(p["start_unix"]).strftime("%Y-%m-%d"), round(minutes, 1),
                     kind, r(hr_med, 1), r(move, 1), now))


# ---------------------------------------------------------------- briefing LLM

PLAUSIBLE = {"hr_min": (30, 120), "hr_mean": (30, 140), "hrv_rmssd": (3, 250), "resp_rate": (5, 35),
             "temp_mean": (25, 40), "temp_dev": (-4, 4), "total": (0, 16), "efficiency": (0, 100)}


# Noms explicites (unités comprises) : un petit modèle confond sinon score de
# sommeil et score de récupération, heures et minutes.
BRIEF_KEYS = {
    "night": "nuit", "score": "score_sommeil", "total": "sommeil_total", "deep": "sommeil_profond",
    "rem": "sommeil_rem", "light": "sommeil_leger", "efficiency": "efficacite_pct", "latency": "latence_min",
    "waso": "eveil_apres_endormissement_min", "awakenings": "reveils_de_plus_de_5_min",
    "timing": "milieu_du_sommeil", "hr_min": "fc_la_plus_basse_bpm",
    "hr_mean": "fc_moyenne_sommeil_bpm", "hrv_rmssd": "hrv_moyen_ms",
    "recovery_index": "indice_de_recuperation", "resp_rate": "respiration_par_min_experimental",
    "temp_mean": "temperature_nuit_c", "temp_dev": "ecart_temperature_c",
    "temp_status": "fiabilite_reference_temperature",
}


HOUR_FIELDS = ("total", "deep", "rem", "light", "recovery_index")
READY_NAMES = {"rhr": "fc_de_repos", "hrv_balance": "equilibre_hrv", "temperature": "temperature",
               "recovery_index": "indice_de_recuperation", "sleep": "sommeil_de_la_nuit",
               "sleep_balance": "equilibre_du_sommeil_14j", "sleep_regularity": "regularite_du_sommeil",
               "previous_day_activity": "activite_de_la_veille", "activity_balance": "equilibre_activite"}


def _hmin(h):
    m = int(round(h * 60))
    return f"{m // 60} h {m % 60:02d}"


def _brief_night(n):
    """Durées déjà formatées (« 7 h 13 ») : en heures décimales, le modèle lit
    7,22 h comme « 7 h 22 »."""
    o = {}
    for k, name in BRIEF_KEYS.items():
        v = n.get(k)
        lo_hi = PLAUSIBLE.get(k)
        if v is None or (lo_hi and not lo_hi[0] <= v <= lo_hi[1]):
            continue
        if k in HOUR_FIELDS:
            v = _hmin(v)
        elif k == "timing":
            m = int(round((v % 24) * 60))
            v = f"{m // 60:02d}:{m % 60:02d}"
        o[name] = v
    return o


REF_METRICS = (("fc_la_plus_basse_bpm", "hr_min", 1), ("fc_moyenne_sommeil_bpm", "hr_mean", 1),
               ("hrv_moyen_ms", "hrv_rmssd", 0), ("respiration_par_min_experimental", "resp_rate", 1),
               ("sommeil_total_h", "total", 2), ("efficacite_pct", "efficiency", 1))


def reference_comparison(night):
    """Valeur de la nuit vs ses références (nuits saines antérieures, ≤ 60 :
    les mêmes que le portail, cf. update_references), calculée ici : un petit
    modèle se trompe dans les soustractions et les sens de variation."""
    refs = json.loads(night.get("ref_json") or "{}")
    out = {}
    for name, key, nd in REF_METRICS:
        v = night.get(key)
        b = refs.get(key)
        if v is None or not b or b["n"] < 3:
            continue
        out[name] = {"nuit": round(v, nd), "reference": round(b["mean"], nd),
                     "ecart": round(v - b["mean"], nd), "nuits_de_reference": b["n"]}
    return out


def briefing_context(der, night, days=14):
    """Contexte du résumé : la nuit à résumer À PART des nuits de comparaison,
    contributeurs réduits à « nom → score » (un modèle de 4 milliards de
    paramètres se perd sinon dans le JSON imbriqué)."""
    upto = [n for n in nights_table(der) if n["night"] <= night]
    nights = upto[-days:]
    if not nights:
        return json.dumps({})
    rd = der.execute("SELECT score, contributors, tension, sleep_debt_h, sleep_need_h FROM readiness"
                     " WHERE day=?", (night,)).fetchone()
    rec = None
    if rd:
        contrib = json.loads(rd[1] or "{}")
        tension = json.loads(rd[2] or "{}")
        rec = {"score_recuperation": rd[0],
               "contributeurs_sur_100": {READY_NAMES.get(k, k): v.get("score") for k, v in contrib.items()
                                         if v.get("score") is not None},
               "signes_de_tension": {
                   "niveau": tension.get("level"),
                   "reference_provisoire": tension.get("provisional"),
                   "ecarts": [{"mesure": x["label"], "valeur": x["value"], "ecart": x["delta"], "unite": x["unit"]}
                              for x in tension.get("signals", [])]}}
        if rd[3] is not None:
            rec["dette_de_sommeil_14j"] = _hmin(rd[3])
            rec["besoin_de_sommeil_retenu"] = _hmin(rd[4] or SLEEP_NEED_H) + " (non personnalisé)"
    tags = [dict(zip(("jour", "type", "note"), t)) for t in der.execute(
        "SELECT day, kind, note FROM tags WHERE day>=? AND day<=? ORDER BY day",
        ((datetime.strptime(night, "%Y-%m-%d") - timedelta(days=2)).strftime("%Y-%m-%d"), night))]
    ctx = {"nuit_a_resumer": _brief_night(nights[-1]),
           "comparaison_a_vos_references": reference_comparison(upto[-1]),
           "recuperation_du_jour": rec,
           "nuits_precedentes_pour_comparaison": [_brief_night(n) for n in nights[:-1]],
           "journal": tags}
    return json.dumps(ctx, ensure_ascii=False)


# nombres légitimes issus de la consigne (seuils, numéros d'urgence…)
PROMPT_NUMBERS = {1, 2, 3, 5, 6, 7, 8, 9, 14, 15, 20, 30, 85, 100, 112}


def unverified_numbers(text, ctx_json):
    """Nombres du texte absents du contexte (tolérance d'arrondi). Garde-fou
    contre les chiffres inventés ou pris sur la mauvaise nuit."""
    import re
    allowed = set(PROMPT_NUMBERS)

    def walk(v, key=""):
        if isinstance(v, bool):
            return
        if isinstance(v, (int, float)):
            for x in (v, abs(v)):
                allowed.update({round(x), round(x, 1), round(x, 2)})
                if key.endswith("_h"):               # durées en heures : « 7 h 13 », « 433 min »
                    allowed.update({round(x * 60), round(x * 60) // 60, round(x * 60) % 60})
        elif isinstance(v, str):
            for m in re.findall(r"\d+", v):
                allowed.add(int(m))
        elif isinstance(v, dict):
            for k, x in v.items():
                walk(x, k)
        elif isinstance(v, list):
            for x in v:
                walk(x, key)
    walk(json.loads(ctx_json))
    bad = []
    for m in re.findall(r"(?<![\w.,])\d+(?:[.,]\d+)?", text):
        x = float(m.replace(",", "."))
        if x in allowed or round(x) in allowed and x == round(x) or round(x, 1) in allowed:
            continue
        if any(abs(x - a) <= 0.051 for a in allowed if isinstance(a, float)):
            continue
        bad.append(m)
    return bad


BRIEFING_SYSTEM = (
    "Vous rédigez le résumé matinal des données de sommeil d'une personne qui porte un anneau Oura "
    "(données calculées localement, sans cloud). Vous recevez en JSON : la nuit à résumer, sa "
    "comparaison aux références personnelles (déjà calculée), la récupération du jour (score, "
    "contributeurs sur 100, signes de tension), les nuits précédentes et le journal (tags saisis "
    "par la personne).\n"
    "Forme : français, VOUVOIEMENT, 5 à 8 phrases en trois temps — (1) la nuit : durée, efficacité, "
    "continuité, et ce qui s'écarte des références ; (2) la récupération : son score et les 2 ou 3 "
    "contributeurs les plus bas, les signes de tension ; (3) une suggestion concrète pour la journée "
    "ou la nuit suivante. Pas de titres ni de listes, 6 à 8 chiffres au maximum.\n"
    "Règles :\n"
    "- n'utilisez QUE les chiffres du contexte, recopiés tels quels ; si une donnée manque, dites-le ;\n"
    "- score_sommeil et score_recuperation sont deux scores DISTINCTS ;\n"
    "- les stades (profond, REM, léger) sont des estimations peu fiables : ne les commentez que "
    "s'ils sont très atypiques, en le précisant ; ne les utilisez jamais pour expliquer la récupération ;\n"
    "- n'affirmez aucune cause : au plus des facteurs POSSIBLES cohérents avec les données ou le "
    "journal (si le journal mentionne une maladie, faites le lien avec prudence) ;\n"
    "- aucun diagnostic médical ;\n"
    "- si une référence repose sur moins de 14 nuits (nuits_de_reference, ou "
    "fiabilite_reference_temperature 'calibrage'/'provisoire'), dites que la comparaison est "
    "encore peu fiable ;\n"
    "- si les signes de tension sont 'marqués', conseillez du repos, de noter les symptômes et de "
    "mesurer sa température avec un thermomètre en cas de malaise ; consulter un médecin si cela "
    "persiste ou en cas de symptômes inquiétants (douleur thoracique, essoufflement : 15 ou 112) ;\n"
    "- ton factuel et bienveillant, sans dramatiser ni rassurer à l'excès."
)


def _swap_base():
    return LLM_URL.split("/v1/")[0]


def running_models():
    """Modèles chargés par llama-swap (None si l'état est illisible)."""
    import urllib.request
    try:
        with urllib.request.urlopen(_swap_base() + "/running", timeout=10) as r:
            return [m.get("model") for m in json.load(r).get("running", []) if m.get("state") == "ready"]
    except Exception:  # noqa: BLE001
        return None


def choose_model():
    """Le 27B s'il est déjà chargé ou si la 3090 est libre (seul le 4B tourne) ;
    sinon le 4B, pour ne jamais décharger un modèle utilisé ailleurs."""
    run = running_models()
    if run is None:
        return LLM_MODEL_FALLBACK
    others = [m for m in run if m not in (LLM_MODEL, LLM_MODEL_FALLBACK)]
    return LLM_MODEL if (LLM_MODEL in run or not others) else LLM_MODEL_FALLBACK


def _llm(model, messages, temperature):
    import urllib.request
    big = model != LLM_MODEL_FALLBACK
    payload = {"model": model, "messages": messages, "temperature": temperature,
               "max_tokens": 6000 if big else 800,
               "chat_template_kwargs": {"reasoning_effort": LLM_REASONING} if big
               else {"enable_thinking": False}}
    req = urllib.request.Request(LLM_URL, data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=900 if big else 240) as resp:
        return (json.load(resp)["choices"][0]["message"].get("content") or "").strip()


def llm_briefing(der, night, ctx, ctx_hash, tries=3, model=None):
    model = model or choose_model()
    messages = [{"role": "system", "content": BRIEFING_SYSTEM + "\nContexte :\n" + ctx},
                {"role": "user", "content": f"Rédige le résumé de la nuit du {night} (champ nuit_a_resumer)."}]
    text, bad = None, []
    for k in range(tries):
        try:
            text = _llm(model, messages, 0.3 if k == 0 else 0.1)
        except Exception as e:  # noqa: BLE001
            print(f"nightly: briefing LLM indisponible : {e}", file=sys.stderr)
            return None
        if not text:
            return None
        bad = unverified_numbers(text, ctx)
        cut = truncated(text)
        if not bad and not cut:
            break
        fix = ("Ces nombres n'existent pas dans le contexte ou ne concernent pas la nuit à résumer : "
               f"{', '.join(bad)}. Réécris le résumé en n'utilisant que les valeurs exactes du contexte "
               "(score_sommeil et score_recuperation sont distincts)." if bad else
               "Le résumé est inachevé. Rédige-le en entier, 5 à 8 phrases terminées.")
        messages += [{"role": "assistant", "content": text}, {"role": "user", "content": fix}]
        bad = bad or ["(texte inachevé)"]
    der.execute("INSERT INTO llm_briefings (night, model, text, ts, input_hash, checked) VALUES (?,?,?,?,?,?)",
                (night, model, text, int(time.time()), ctx_hash, 0 if bad else 1))
    if bad:
        print(f"nightly: résumé conservé NON vérifié (chiffres introuvables : {', '.join(bad)})", file=sys.stderr)
    return text


def truncated(text):
    """Résumé manifestement coupé (budget de jetons consommé par le raisonnement :
    « …efficacité de 89 ») : trop court ou sans ponctuation finale."""
    t = (text or "").strip()
    return len(t) < 200 or t[-1] not in ".!?…»)"


def briefing_hash(ctx):
    """Empreinte des éléments qui justifient une réécriture : la nuit, les
    comparaisons, le score et les signes de tension, le journal. Les petites
    variations des contributeurs (activité de la veille recalculée) n'en font
    pas partie : chaque réécriture coûte un appel au 27B."""
    c = json.loads(ctx)
    rec = c.get("recuperation_du_jour") or {}
    key = {"n": c.get("nuit_a_resumer"), "cmp": c.get("comparaison_a_vos_references"),
           "s": rec.get("score_recuperation"), "t": (rec.get("signes_de_tension") or {}).get("niveau"),
           "j": c.get("journal")}
    return hashlib.sha1(json.dumps(key, sort_keys=True).encode()).hexdigest()[:16]


def maybe_briefing(der, latest, now, upgrade_window_s=18 * 3600, log=print):
    """(Ré)écrit le résumé de la dernière nuit, une fois la nuit CLOSE (signe
    d'éveil observé après la fin de sa fenêtre, cf. night_closed) :
    - entrées modifiées → réécriture ;
    - résumé écrit par le 4B faute de 3090 libre → réécrit par le 27B dès qu'il
      redevient disponible (pendant 18 h après le réveil)."""
    stop = latest.get("stop_unix") or now
    if latest.get("provisional"):
        return
    ctx = briefing_context(der, latest["night"])
    h = briefing_hash(ctx)
    last = der.execute("SELECT input_hash, model FROM llm_briefings WHERE night=? ORDER BY ts DESC LIMIT 1",
                       (latest["night"],)).fetchone()
    model = choose_model()
    if last and last[0] == h:
        upgrade = (last[1] != LLM_MODEL and model == LLM_MODEL and now - stop < upgrade_window_s)
        if not upgrade:
            return
    txt = llm_briefing(der, latest["night"], ctx, h, model=model)
    if txt:
        verb = "généré" if not last else ("réécrit par " + model if last[0] == h else "régénéré")
        log(f"nightly: briefing LLM {verb} pour {latest['night']} ({model}, {len(txt)} car.)")


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

    # nuit en cours ? (seule la dernière peut l'être)
    der.execute("UPDATE sleep_scores SET provisional=0")
    if nights_raw:
        last = nights_raw[-1]
        closed, _why = night_closed(con, axis, last, now)
        der.execute("UPDATE sleep_scores SET provisional=? WHERE night=?", (int(not closed), last["night"]))
        if not closed:
            log(f"nightly: nuit {last['night']} en cours (aucun signe d'éveil après "
                f"{datetime.fromtimestamp(last['end_unix']).strftime('%H:%M')}) — provisoire, pas de résumé")

    nights = nights_table(der)
    update_references(der, nights)
    update_baselines(der, nights, now)
    rests = oc.rest_periods(con, axis, nights_raw)
    activity = update_activity(con, der, axis, nights, charging, recompute_all, now, rests)
    update_readiness(der, nights, activity, now)
    update_rests(con, der, nights_raw, nights, rests, now)
    der.commit()

    if briefing and nights:
        maybe_briefing(der, nights[-1], now, log=log)
        der.commit()
    for (night,) in der.execute("SELECT DISTINCT night FROM llm_briefings").fetchall():
        if night in {n["night"] for n in nights}:
            der.execute("UPDATE sleep_scores SET brief_hash=? WHERE night=?",
                        (briefing_hash(briefing_context(der, night)), night))
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
