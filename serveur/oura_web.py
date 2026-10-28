#!/usr/bin/env python3
"""oura-web — portail des données de l'anneau Oura (FastAPI, sans étape de build).

Lit en lecture seule :
  oura.db     — événements bruts décodés (open_oura) + push_telemetry
  derived.db  — dérivations du job nightly (derive_night.py) : nuits, hypnogrammes,
                séries, lignes de base, récupération, activité, résumés LLM
Écrit uniquement :
  derived.db  — profil utilisateur et journal (tags)
  oura.db     — POST /ingest/events (envoi du téléphone)

L'interface (static/) est du HTML/CSS/JS sans dépendance. Le chat passe par
llama-swap (API compatible OpenAI) en local.
"""
import asyncio
import base64
import hashlib
from contextlib import asynccontextmanager
import hmac
import json
import os
import secrets
import sqlite3
import time
from datetime import datetime, timedelta
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

import oura_core as oc

RAW_DB = Path(os.environ.get("OURA_RAW_DB", "/srv/oura/oura.db"))
DERIVED_DB = Path(os.environ.get("OURA_DERIVED_DB", "/srv/oura/derived.db"))
INBOX_DIR = Path(os.environ.get("OURA_INBOX", "/srv/oura/inbox"))
STATIC_DIR = Path(__file__).with_name("static")
LLM_URL = os.environ.get("OURA_LLM_URL", "http://127.0.0.1:8012/v1/chat/completions")
LLM_MODEL = os.environ.get("OURA_LLM_MODEL_FALLBACK", "qwen3.5-4b")      # petit modèle, toujours chargé
# « Analyse approfondie » : Ling-3.0-flash-Sante (MoE médical 124B-A5.1B, IQ2_M,
# entrée llama-swap « ling-sante »), chargé à la demande sur la 3090 (~35 s, il
# décharge le qwen3.8-27b résident). Le résumé du matin reste au 27B (derive_night).
# Variables distinctes de celles du nightly (OURA_LLM_MODEL / OURA_LLM_MAY_SWAP).
LLM_MODEL_DEEP = os.environ.get("OURA_CHAT_MODEL_DEEP", "ling-sante")
# 1 : l'analyse approfondie peut reprendre la 3090 même si un autre modèle l'occupe
# (0 : 409 « 3090 occupée », comportement prévu pour un modèle résident)
LLM_MAY_SWAP = os.environ.get("OURA_CHAT_MAY_SWAP", "1") == "1"
DATE_RE = r"^\d{4}-\d{2}-\d{2}$"

# Nom d'événement de repli à l'ingestion (colonne `name` NOT NULL) quand la
# source n'en fournit pas. Conservé tel quel : la déduplication ne dépend pas
# du nom (UNIQUE(serial, tag, ring_timestamp, body)).
TAG_NAMES = {65: "ring_start", 66: "time_sync", 69: "state_change", 70: "temp_event",
             71: "motion_event", 80: "activity_information", 83: "wear_event",
             91: "ble_connection", 93: "hrv_event", 96: "ibi_and_amplitude",
             97: "debug_data", 107: "motion_period", 128: "ibi_hr", 139: "spo2_r_pi"}

# Jeton par source au-dessus de l'authentification basique nginx (en-tête
# X-Oura-Token). JAMAIS de valeur en dur : le secret vient de l'environnement
# du service (EnvironmentFile=/etc/oura/ingest.env). Sans variable, l'ingestion
# refuse tout plutôt que d'accepter un jeton connu.
INGEST_TOKENS = {
    s: t for s, t in {
        "phone": os.environ.get("OURA_PHONE_TOKEN", ""),
        "pc": os.environ.get("OURA_PC_TOKEN", ""),
    }.items() if t
}

TAG_KINDS = {
    "alcool": "Alcool", "cafeine": "Caféine tardive", "repas": "Repas tardif",
    "sport": "Sport intense", "maladie": "Malade / symptômes", "stress": "Stress",
    "voyage": "Voyage / décalage", "medicament": "Médicament", "ecran": "Écrans tardifs",
    "sieste": "Sieste", "autre": "Autre",
}

def ensure_push_telemetry():
    """Table de fraîcheur des envois (une ligne par source)."""
    if RAW_DB.exists():
        c = sqlite3.connect(str(RAW_DB), timeout=15)
        try:
            c.execute("""CREATE TABLE IF NOT EXISTS push_telemetry (
                source TEXT PRIMARY KEY, last_push_unix INTEGER, cursor INTEGER,
                events_pushed INTEGER, last_status TEXT)""")
            c.commit()
        finally:
            c.close()


@asynccontextmanager
async def lifespan(_app):
    ensure_push_telemetry()
    yield


app = FastAPI(title="oura-web", lifespan=lifespan)


# ---------------------------------------------------------------- accès aux bases

def q(c, sql, args=()):
    c.row_factory = sqlite3.Row
    return [dict(r) for r in c.execute(sql, args).fetchall()]


def one(c, sql, args=()):
    rows = q(c, sql, args)
    return rows[0] if rows else None


def safe_q(c, sql, args=()):
    """Requête tolérante à une table absente (derived.db pas encore migré)."""
    try:
        return q(c, sql, args)
    except sqlite3.OperationalError:
        return []


def safe_one(c, sql, args=()):
    rows = safe_q(c, sql, args)
    return rows[0] if rows else None


def raw():
    if not RAW_DB.exists():
        raise HTTPException(503, "oura.db absent — en attente du premier envoi")
    c = sqlite3.connect(f"file:{RAW_DB}?mode=ro", uri=True)
    c.execute("PRAGMA busy_timeout=3000")
    return c


def derived():
    if not DERIVED_DB.exists():
        return None
    c = sqlite3.connect(f"file:{DERIVED_DB}?mode=ro", uri=True)
    c.execute("PRAGMA busy_timeout=3000")
    return c


def derived_rw():
    """derived.db en écriture : profil et journal uniquement (jamais recalculés
    par le nightly)."""
    c = sqlite3.connect(str(DERIVED_DB))
    c.execute("PRAGMA busy_timeout=5000")
    c.executescript("""
        CREATE TABLE IF NOT EXISTS user_profile (
            id INTEGER PRIMARY KEY CHECK (id=1), age_years INTEGER, height_cm INTEGER,
            weight_kg REAL, sex TEXT, updated_unix INTEGER);
        CREATE TABLE IF NOT EXISTS tags (
            id INTEGER PRIMARY KEY AUTOINCREMENT, day TEXT NOT NULL, kind TEXT NOT NULL,
            note TEXT, created_unix INTEGER);
    """)
    return c


_axis_cache: tuple | None = None     # (clé, TimeAxis)


def axis_for(c) -> oc.TimeAxis:
    """TimeAxis mis en cache tant qu'aucun événement n'a été ajouté."""
    global _axis_cache
    key = c.execute("SELECT MAX(id), COUNT(*) FROM events").fetchone()
    if _axis_cache is None or _axis_cache[0] != key:
        _axis_cache = (key, oc.TimeAxis(c))
    return _axis_cache[1]


def jparse(v, default=None):
    if not v:
        return default
    try:
        return json.loads(v)
    except (TypeError, ValueError):
        return default


def night_row(r):
    if r is None:
        return None
    r = dict(r)
    r["subscores"] = jparse(r.get("subscores"), {})
    r["ref"] = jparse(r.pop("ref_json", None), None)
    return r


def baselines(d):
    return {b["metric"]: b for b in safe_q(d, "SELECT * FROM baselines")} if d else {}


def night_baselines(d, n):
    """Références d'une nuit : celles figées à son calcul (nuits saines
    antérieures), sinon les lignes de base courantes (base dérivée ancienne)."""
    ref = n.pop("ref", None) if n else None
    if ref is not None:                  # {} = aucune nuit de référence (première nuit)
        return {m: dict(b, metric=m) for m, b in ref.items()}
    return baselines(d)


def readiness_row(r):
    if not r:
        return None
    return {"day": r["day"], "score": r["score"], "rating": oc.rating(r["score"]),
            "contributors": jparse(r["contributors"], {}), "tension": jparse(r["tension"], {}),
            "n_base": r["n_base"], "provisional": bool(r["provisional"]),
            "sleep_debt_h": r.get("sleep_debt_h"), "sleep_debt_n": r.get("sleep_debt_n"),
            "sleep_need_h": r.get("sleep_need_h")}


def activity_row(r):
    if not r:
        return None
    r = dict(r)
    r["contributors"] = jparse(r.get("contributors"), {})
    r["rating"] = oc.rating(r.get("score"))
    return r


# ---------------------------------------------------------------- état de l'anneau

def latest_event(c, tags, need_json=True):
    ph = ",".join("?" * len(tags))
    extra = " AND decoded_json IS NOT NULL" if need_json else ""
    r = one(c, f"SELECT id, tag, name, ring_timestamp rt, captured_unix cap, decoded_json dj"
               f" FROM events WHERE tag IN ({ph}){extra} ORDER BY ring_timestamp DESC, id DESC LIMIT 1",
            tuple(tags))
    if r:
        r["decoded"] = oc.jget(r.pop("dj"))
    return r


def latest_battery(c):
    r = one(c, "SELECT decoded_json dj, ring_timestamp rt, captured_unix cap FROM events"
               " WHERE tag=97 AND decoded_json LIKE '%battery_level_changed%'"
               " ORDER BY ring_timestamp DESC LIMIT 1")
    if not r:
        return None
    j = oc.jget(r["dj"]) or {}
    return {"pct": j.get("battery_pct"), "mv": j.get("voltage_mv"), "rt": r["rt"], "cap": r["cap"]}


def battery_summary(c, ax, days=30, charging=None):
    """Historique (%), charges et autonomie : pente de décharge du segment en cours
    (régression, ≥ 6 h depuis la dernière charge) ou, à défaut, pente typique des
    segments précédents ; heures restantes jusqu'à 0 % et jusqu'à 20 %."""
    now = time.time()
    since = now - days * 86400
    pts = oc.battery_points(c, ax, since)
    charging = oc.charging_intervals_ds(c) if charging is None else charging
    ch = [(ax(a), ax(b)) for a, b in charging]
    ch = [(a, b) for a, b in ch if a and b and b >= since - 86400]
    cur, typ = oc.discharge_rate(pts, ch)
    rate = cur or typ
    last = pts[-1] if pts else None
    out = {"points": [{"t": t, "pct": p, "mv": mv} for t, p, mv in pts], "charges": ch,
           "rate_pct_h": round(rate, 3) if rate else None, "rate_source": "en cours" if cur else "typique",
           "typical_rate_pct_h": round(typ, 3) if typ else None,
           "full_days": round(100 / rate / 24, 1) if rate else None,
           "pct": last[1] if last else None, "t": last[0] if last else None,
           "remaining_h": None, "to_20_h": None}
    if last and rate:
        elapsed = max(0.0, now - last[0]) / 3600
        pct_now = max(0.0, last[1] - rate * elapsed)
        out["remaining_h"] = round(pct_now / rate, 1)
        out["to_20_h"] = round(max(0.0, pct_now - 20) / rate, 1)
    return out


def ring_status(c, ax):
    dev = one(c, "SELECT serial, firmware, api_version FROM device ORDER BY updated_unix DESC LIMIT 1")
    bat = latest_battery(c)
    if bat:
        bat["t"] = ax(bat.pop("rt"), bat.pop("cap"))
        bs = battery_summary(c, ax, days=12)
        bat.update({k: bs[k] for k in ("rate_pct_h", "rate_source", "remaining_h", "to_20_h", "full_days")})
    st = latest_event(c, [oc.T_STATE, oc.T_WEAR])
    state = None
    if st:
        j = st["decoded"] or {}
        state = {"label": oc.state_label(st["tag"], j.get("state"), j.get("text")),
                 "t": ax(st["rt"], st["cap"])}
    charging = oc.charging_intervals_ds(c)
    last_rt = c.execute("SELECT MAX(ring_timestamp) FROM events").fetchone()[0]
    on_charger = bool(charging and last_rt and charging[-1][0] <= last_rt <= charging[-1][1])
    temp = latest_event(c, [oc.T_TEMP])
    skin = None
    if temp:
        v = oc.skin_temp(temp["decoded"])
        skin = {"v": v, "t": ax(temp["rt"], temp["cap"])}
    worn = None if skin is None or skin["v"] is None else (skin["v"] > 30.0 and not on_charger)
    push = {r["source"]: r for r in safe_q(c, "SELECT * FROM push_telemetry")}
    ts = one(c, "SELECT captured_unix cap, decoded_json dj FROM events WHERE tag=66"
                " ORDER BY ring_timestamp DESC LIMIT 1")
    return {
        "device": dev, "battery": bat, "state": state, "on_charger": on_charger, "worn": worn,
        "skin_temp": skin,
        "last_capture": c.execute("SELECT MAX(captured_unix) FROM events").fetchone()[0],
        "push": push,
        "last_time_sync": (oc.jget(ts["dj"]) or {}).get("unix_time") if ts else None,
        "axis": {"mode": ax.mode, "drift_s": ax.drift_s},
        "total_events": c.execute("SELECT COUNT(*) FROM events").fetchone()[0],
    }


def live_signals(c, ax):
    out = {}
    # dernière mesure de FC diurne exploitable (≥ 5 battements de bonne qualité)
    for r in q(c, "SELECT ring_timestamp rt, captured_unix cap, decoded_json dj FROM events"
                  " WHERE tag=? ORDER BY ring_timestamp DESC LIMIT 40", (oc.T_GREEN_IBI,)):
        hrs = sorted(b for b in ((oc.jget(r["dj"]) or {}).get("hr_bpm") or [])
                     if oc.HR_MIN_BPM <= b <= oc.HR_MAX_BPM)
        if len(hrs) >= 5:
            out["hr"] = {"v": hrs[len(hrs) // 2], "t": ax(r["rt"], r["cap"])}
            break
    a = latest_event(c, [oc.T_ACTIVITY])
    if a and (a["decoded"] or {}).get("met"):
        out["met"] = {"v": a["decoded"]["met"][-1], "t": ax(a["rt"], a["cap"])}
    return out


# ---------------------------------------------------------------- API : synthèse

@app.get("/api/overview")
def overview():
    c = raw()
    ax = axis_for(c)
    ring = ring_status(c, ax)
    live = live_signals(c, ax)
    c.close()
    d = derived()
    out = {"now": int(time.time()), "ring": ring, "live": live, "night": None, "readiness": None,
           "activity": None, "briefing": None, "baselines": {}}
    if d:
        n = night_row(safe_one(d, "SELECT * FROM sleep_scores ORDER BY night DESC LIMIT 1"))
        out["night"] = n
        out["baselines"] = night_baselines(d, n)
        if n:
            out["staging"] = staging_payload(d, n)
            out["readiness"] = readiness_row(safe_one(d, "SELECT * FROM readiness WHERE day=?", (n["night"],)))
        today = oc.activity_day(time.time())          # journée d'activité : 04:00 → 03:59
        out["activity"] = activity_row(safe_one(d, "SELECT * FROM activity_daily WHERE day=?", (today,)))
        yday = oc.activity_day(time.time() - 86400)
        out["activity_yesterday"] = activity_row(safe_one(d, "SELECT * FROM activity_daily WHERE day=?", (yday,)))
        b = safe_one(d, "SELECT * FROM llm_briefings ORDER BY ts DESC LIMIT 1")
        if b:
            # « données modifiées » : les entrées du résumé (nuit, références, score,
            # tension, journal) diffèrent de celles d'aujourd'hui, pas un simple recalcul
            b["stale"] = briefing_stale(b, n)
        out["briefing"] = b
        d.close()
    return out


def briefing_stale(b, n):
    return bool(n and b and b.get("night") == n.get("night") and n.get("brief_hash")
                and b.get("input_hash") != n["brief_hash"])


def briefing_with_state(b, n):
    if b:
        b["stale"] = briefing_stale(b, n)
    return b


def staging_payload(d, n):
    st = safe_q(d, "SELECT stage FROM night_staging WHERE night=? ORDER BY epoch", (n["night"],))
    code = {"awake": "W", "light": "L", "deep": "D", "rem": "R"}
    return {"start": n.get("start_unix"), "epoch_s": 30,
            "stages": "".join(code.get(s["stage"], "L") for s in st)}


@app.get("/api/nights")
def nights():
    d = derived()
    if not d:
        return {"nights": []}
    rows = safe_q(d, "SELECT night, score, total, deep, rem, efficiency, hr_min, hr_mean, hrv_rmssd,"
                     " temp_dev, temp_status, resp_rate FROM sleep_scores ORDER BY night")
    d.close()
    return {"nights": rows}


@app.get("/api/night")
def night(date: str = Query(..., pattern=DATE_RE)):
    d = derived()
    if not d:
        raise HTTPException(503, "derived.db absent")
    n = night_row(safe_one(d, "SELECT * FROM sleep_scores WHERE night=?", (date,)))
    if not n:
        d.close()
        raise HTTPException(404, f"nuit {date} inconnue")
    prev = safe_one(d, "SELECT night FROM sleep_scores WHERE night<? ORDER BY night DESC LIMIT 1", (date,))
    nxt = safe_one(d, "SELECT night FROM sleep_scores WHERE night>? ORDER BY night LIMIT 1", (date,))
    prev_row = safe_one(d, "SELECT score, total, hr_min, hrv_rmssd FROM sleep_scores WHERE night=?",
                        (prev["night"],)) if prev else None
    series = safe_q(d, "SELECT t, hr, rmssd, resp, motion, temp FROM night_series WHERE night=? ORDER BY t", (date,))
    day0 = (datetime.strptime(date, "%Y-%m-%d") - timedelta(days=1)).strftime("%Y-%m-%d")
    out = {
        "night": n, "prev": prev["night"] if prev else None, "next": nxt["night"] if nxt else None,
        "prev_row": prev_row, "staging": staging_payload(d, n), "series": series,
        "baselines": night_baselines(d, n),
        "readiness": readiness_row(safe_one(d, "SELECT * FROM readiness WHERE day=?", (date,))),
        "briefing": briefing_with_state(safe_one(d, "SELECT * FROM llm_briefings WHERE night=?"
                                               " ORDER BY ts DESC LIMIT 1", (date,)), n),
        "tags": safe_q(d, "SELECT * FROM tags WHERE day IN (?,?) ORDER BY created_unix", (day0, date)),
    }
    d.close()
    return out


@app.get("/api/readiness")
def readiness(days: int = Query(30, ge=1, le=365), date: str | None = Query(None, pattern=DATE_RE)):
    d = derived()
    if not d:
        return {"days": []}
    if date:
        r = readiness_row(safe_one(d, "SELECT * FROM readiness WHERE day=?", (date,)))
        d.close()
        if not r:
            raise HTTPException(404, f"pas de récupération pour {date}")
        return r
    rows = [readiness_row(r) for r in safe_q(d, "SELECT * FROM readiness ORDER BY day DESC LIMIT ?", (days,))]
    d.close()
    return {"days": list(reversed(rows))}


@app.get("/api/trends")
def trends(days: int = Query(90, ge=7, le=365)):
    d = derived()
    if not d:
        raise HTTPException(503, "derived.db absent")
    rows = safe_q(d, "SELECT night, score, total, deep, rem, light, awake, efficiency, latency, waso,"
                     " timing, hr_min, hr_mean, hrv_rmssd, recovery_index, resp_rate, temp_mean, temp_dev,"
                     " temp_status, restfulness, start_unix, stop_unix, onset_unix, end_unix, provisional,"
                     " ref_excluded FROM sleep_scores ORDER BY night DESC LIMIT ?", (days,))
    rd = {r["day"]: r for r in safe_q(d, "SELECT day, score, sleep_debt_h FROM readiness")}
    act = safe_q(d, "SELECT day, met_min_mh, inactive_min, active_kcal, total_kcal, steps, hr_day_rest,"
                    " hr_day_mean, score, partial FROM activity_daily ORDER BY day DESC LIMIT ?", (days,))
    for r in rows:
        x = rd.get(r["night"]) or {}
        r["readiness"], r["sleep_debt_h"] = x.get("score"), x.get("sleep_debt_h")
    since = rows[-1]["night"] if rows else "9999"
    tags_q = safe_q(d, "SELECT day, kind, note FROM tags WHERE day>=? ORDER BY day",
                    ((datetime.strptime(since, "%Y-%m-%d") - timedelta(days=1)).strftime("%Y-%m-%d")
                     if rows else since,))
    out = {"nights": list(reversed(rows)), "activity": list(reversed(act)), "baselines": baselines(d),
           "tags": tags_q, "tag_kinds": TAG_KINDS}
    d.close()
    return out


@app.get("/api/briefing")
def briefing(date: str | None = Query(None, pattern=DATE_RE)):
    d = derived()
    if not d:
        raise HTTPException(503, "derived.db absent")
    row = (safe_one(d, "SELECT * FROM llm_briefings WHERE night=? ORDER BY ts DESC LIMIT 1", (date,))
           if date else safe_one(d, "SELECT * FROM llm_briefings ORDER BY ts DESC LIMIT 1"))
    d.close()
    if not row:
        raise HTTPException(404, "aucun résumé")
    return row


# ---------------------------------------------------------------- API : activité

@app.get("/api/activity")
def activity(date: str | None = Query(None, pattern=DATE_RE)):
    """Journée d'activité (04:00 → 03:59, comme Oura) : bins MET 1 min, pas, FC,
    tranches d'effort, sommeil, repos, charge, minutes non portées."""
    c = raw()
    ax = axis_for(c)
    day = date or oc.activity_day(time.time())
    t0, t1 = oc.activity_day_bounds(day)
    am = oc.activity_minutes(c, ax, t0, t1)
    met_list = [{"t": t, "met": v["met"], "steps": round(v["steps"], 1)} for t, v in sorted(am.items())]
    steps_h = {}
    for p in met_list:
        k = int(p["t"] // 3600) * 3600
        steps_h[k] = steps_h.get(k, 0.0) + p["steps"]

    # FC de jour : médiane par minute des mesures de l'anneau (tag 128, LED verte)
    s_ds, e_ds = ax.to_ds(t0 - 600), ax.to_ds(t1 + 600)
    hr = [{"t": t + 30, "hr": round(v)} for t, v in oc.day_hr_minutes(c, ax, t0, t1).items()]
    # FC de nuit : bins 5 min de l'anneau
    for cds, h, _ in oc.hrv_bins(c, s_ds, e_ds):
        t = ax(cds)
        if h is not None and t is not None and t0 <= t < t1:
            hr.append({"t": t, "hr": h, "night": True})
    hr.sort(key=lambda p: p["t"])

    # tranches d'effort : minutes à MET ≥ 3, trou toléré ≤ 10 min, ≥ 5 min
    segs, cur = [], None
    for p in met_list:
        if p["met"] >= 3.0:
            if cur is None or p["t"] - cur["end"] > 600:
                if cur and cur["end"] - cur["start"] >= 300 and cur["n"] >= 4:
                    segs.append(cur)
                cur = {"start": p["t"], "end": p["t"], "s": p["met"], "n": 1}
            else:
                cur["end"] = p["t"]
                cur["s"] += p["met"]
                cur["n"] += 1
    if cur and cur["end"] - cur["start"] >= 300 and cur["n"] >= 4:
        segs.append(cur)
    for s in segs:
        s["min"] = round((s["end"] - s["start"]) / 60 + 1)
        s["met_avg"] = round(s.pop("s") / s.pop("n"), 1)
        inside = [p["hr"] for p in hr if s["start"] <= p["t"] <= s["end"] + 60]
        s["hr_mean"] = round(sum(inside) / len(inside)) if inside else None
        s["hr_max"] = max(inside) if inside else None

    nights_raw = oc.load_nights(c, ax)
    sleep = [(n["start_unix"], n["end_unix"]) for n in nights_raw
             if n["end_unix"] > t0 and n["start_unix"] < t1]
    rest_p = oc.rest_periods(c, ax, nights_raw)
    rests = [(p["start_unix"], p["end_unix"]) for p in rest_p if p["end_unix"] > t0 and p["start_unix"] < t1]
    charging_ds = oc.charging_intervals_ds(c)
    charging = []
    for a, b in charging_ds:
        ta, tb = ax(a), ax(b)
        if ta and tb and tb > t0 and ta < t1:
            charging.append((ta, tb))
    # minutes non portées (ni chaleur cutanée, ni mouvement, ni activité) et retraits
    grid = oc.activity_grid(c, ax, t0, min(t1, time.time()), sleep, rests, charging,
                            oc.wear_removals(c, ax), charging_ds)
    nonworn = oc.grid_runs(grid, ("non porté", "retiré"))
    c.close()
    dd = derived()
    summary = activity_row(safe_one(dd, "SELECT * FROM activity_daily WHERE day=?", (day,))) if dd else None
    prof = safe_one(dd, "SELECT weight_kg, age_years, height_cm, sex FROM user_profile WHERE id=1") if dd else None
    rest_list = safe_q(dd, "SELECT start_unix, end_unix, minutes, kind, hr_median, move_pct FROM rests"
                           " WHERE end_unix>? AND start_unix<? ORDER BY start_unix", (t0, t1)) if dd else []
    if dd:
        dd.close()
    return {"date": day, "mode": ax.mode, "met": met_list, "hr": hr, "segments": segs,
            "steps_hourly": [{"t": k, "steps": round(v)} for k, v in sorted(steps_h.items())],
            "t0": t0, "t1": t1, "nonworn": nonworn,
            "sleep": sleep, "rests": rests, "rest_list": rest_list, "charging": charging, "summary": summary,
            "weight_kg": prof["weight_kg"] if prof else None,
            "profile_complete": bool(prof and prof["age_years"] and prof["height_cm"] and prof["weight_kg"])}


@app.get("/api/battery")
def battery(days: int = Query(14, ge=1, le=90)):
    c = raw()
    ax = axis_for(c)
    out = battery_summary(c, ax, days)
    c.close()
    out["since"] = time.time() - days * 86400
    return out


@app.get("/api/quality")
def quality(days: int = Query(30, ge=7, le=365)):
    """Qualité des données par nuit et par jour (couverture des signaux)."""
    d = derived()
    if not d:
        raise HTTPException(503, "derived.db absent")
    nights_q = safe_q(d, "SELECT night, provisional, axis_mode, hr_source, staging_source, ref_excluded,"
                         " beats_kept_pct, lead_min, hr_bins_pct, resp_n, in_bed FROM sleep_scores"
                         " ORDER BY night DESC LIMIT ?", (days,))
    days_q = safe_q(d, "SELECT day, partial, worn_min, awake_min, nonworn_min, met_cover_pct, hr_day_n, charge_min, rest_min"
                       " FROM activity_daily ORDER BY day DESC LIMIT ?", (days,))
    d.close()
    return {"nights": nights_q, "days": days_q}


# ---------------------------------------------------------------- cellier
# Onglet protégé par un 2e mot de passe (haché scrypt, jamais en clair).
# Le hash vient de l'environnement du service (EnvironmentFile=
# /etc/oura/ingest.env : CELLIER_HASH=scrypt$<n>$<r>$<p>$<hexsalt>$<hexhash>),
# généré par : python3 serveur/cellier_pass.py   (mot de passe via CELLIER_PASS).
# Session : cookie HttpOnly signé avec le hash, valide 7 jours.
CELLIER_COOKIE = "cellier"
CELLIER_TTL = 7 * 86400
CELLIER_HASH = os.environ.get("CELLIER_HASH", "")

def _cellier_configured():
    return bool(CELLIER_HASH)

def _cellier_check(password):
    try:
        algo, n, r, p, salt, hashv = CELLIER_HASH.split("$")
    except (ValueError, AttributeError):
        return False
    if algo != "scrypt":
        return False
    try:
        digest = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt),
                                n=int(n), r=int(r), p=int(p), dklen=32)
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(digest, bytes.fromhex(hashv))

def _cellier_token(exp):
    return f"{int(exp)}:" + hmac.new(CELLIER_HASH.encode(), str(int(exp)).encode(),
                                     hashlib.sha256).hexdigest()[:32]

def _cellier_authorized(request: Request):
    if not CELLIER_HASH:
        return False
    c = request.cookies.get(CELLIER_COOKIE, "")
    try:
        exp, sig = c.rsplit(":", 1)
        exp = int(exp)
    except (ValueError, AttributeError):
        return False
    if exp < time.time():
        return False
    return hmac.compare_digest(_cellier_token(exp), c)

@app.post("/api/cellier/auth")
async def cellier_auth(request: Request):
    try:
        body = await request.json()
    except Exception:
        body = {}
    if not _cellier_configured():
        raise HTTPException(503, "cellier non configuré")
    if not _cellier_check(body.get("password", "")):
        raise HTTPException(401, "mot de passe incorrect")
    exp = time.time() + CELLIER_TTL
    return JSONResponse({"ok": True}, headers={
        "Set-Cookie": f"{CELLIER_COOKIE}={_cellier_token(exp)}; Path=/; HttpOnly; "
                       f"SameSite=Lax; Max-Age={CELLIER_TTL}"})

@app.post("/api/cellier/logout")
def cellier_logout():
    return JSONResponse({"ok": True},
                        headers={"Set-Cookie": f"{CELLIER_COOKIE}=; Path=/; Max-Age=0"})

@app.get("/api/cellier")
def cellier(request: Request):
    """Liste des épisodes détectés (derive_cellier.py, recalculé en nightly)."""
    if not _cellier_configured():
        raise HTTPException(503, "cellier non configuré")
    if not _cellier_authorized(request):
        raise HTTPException(401, "authentification requise")
    d = derived()
    if not d:
        raise HTTPException(503, "derived.db absent")
    tables = {r[0] for r in d.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    d.close()
    if "cellier_episodes" not in tables:
        return {"episodes": [], "configured": True}   # première nightly pas encore passée
    d = derived()
    if not d:
        raise HTTPException(503, "derived.db absent")
    eps = safe_q(d, "SELECT start_unix, end_unix, duration_min, hr_mean, hr_max, peak_min,"
                       " met_mean, skin_mean, skin_before, skin_delta, confidence, day"
                       " FROM cellier_episodes ORDER BY start_unix DESC LIMIT 500")
    d.close()
    return {"episodes": eps, "configured": True}


# ---------------------------------------------------------------- API : signaux bruts

@app.get("/api/telemetry")
def telemetry(hours: int = Query(6, ge=1, le=168)):
    c = raw()
    ax = axis_for(c)
    now = time.time()
    since = now - hours * 3600
    s_ds = ax.to_ds(since - 3600)
    charging = oc.charging_intervals_ds(c)

    def evs(tag):
        return q(c, "SELECT ring_timestamp rt, captured_unix cap, decoded_json dj FROM events"
                    " WHERE tag=? AND ring_timestamp>=? ORDER BY ring_timestamp", (tag, s_ds))

    temp = []
    for r in evs(oc.T_TEMP):
        t = ax(r["rt"], r["cap"])
        v = oc.skin_temp(oc.jget(r["dj"]))
        if t and t >= since and v is not None and not oc.in_intervals(r["rt"], charging):
            temp.append({"t": t, "v": v})
    motion = []
    for r in evs(oc.T_MOTION):
        t = ax(r["rt"], r["cap"])
        j = oc.jget(r["dj"]) or {}
        if t and t >= since:
            motion.append({"t": t, "seconds": j.get("motion_seconds") or 0,
                           "high": j.get("high_intensity") or 0})
    hr = []
    for cds, h, rm in oc.hrv_bins(c, s_ds, 1 << 62):
        t = ax(cds)
        if t and t >= since and h is not None:
            hr.append({"t": t, "hr": h, "rmssd": rm})
    for r in evs(oc.T_GREEN_IBI):
        j = oc.jget(r["dj"]) or {}
        beats = sorted(b for b in (j.get("hr_bpm") or []) if oc.HR_MIN_BPM <= b <= oc.HR_MAX_BPM)
        t = ax(r["rt"], r["cap"])
        if len(beats) >= 5 and t and t >= since:
            hr.append({"t": t, "hr": beats[len(beats) // 2], "rmssd": None})
    hr.sort(key=lambda p: p["t"])
    # SpO2 : rapport R brut (non étalonné) moyenné par minute — 1 Hz serait
    # illisible et n'apporte rien de plus
    buckets = {}
    for r in evs(oc.T_SPO2_RPI):
        t_end = ax(r["rt"], r["cap"])
        rr = (oc.jget(r["dj"]) or {}).get("r") or []
        if not t_end:
            continue
        n = len(rr)
        for k, v in enumerate(rr):
            t = t_end - (n - k)
            if t >= since and 0.2 <= v <= 2.0:
                buckets.setdefault(int(t // 60), []).append(v)
    spo2 = [{"t": m * 60 + 30, "r": round(sum(v) / len(v), 3)} for m, v in sorted(buckets.items())]
    states = []
    for r in q(c, "SELECT tag, ring_timestamp rt, captured_unix cap, decoded_json dj FROM events"
                  " WHERE tag IN (?,?) AND ring_timestamp>=? ORDER BY ring_timestamp",
               (oc.T_STATE, oc.T_WEAR, s_ds)):
        t = ax(r["rt"], r["cap"])
        j = oc.jget(r["dj"]) or {}
        if t and t >= since:
            states.append({"t": t, "label": oc.state_label(r["tag"], j.get("state"), j.get("text"))})
    ch = [(ax(a), ax(b)) for a, b in charging]
    ch = [(a, b) for a, b in ch if a and b and b >= since]
    c.close()
    return {"since": since, "now": now, "anchored": ax.mode, "drift_s": ax.drift_s,
            "temp": temp, "motion": motion, "hr": hr, "spo2": spo2, "states": states, "charging": ch}


@app.get("/api/events")
def events(limit: int = Query(100, ge=1, le=500), type: str | None = None, all: bool = False):
    c = raw()
    ax = axis_for(c)
    sql = "SELECT tag, name, ring_timestamp rt, captured_unix cap, decoded_json dj FROM events"
    args = []
    if type:
        sql += " WHERE name=?"
        args.append(type)
    elif not all:
        sql += " WHERE name NOT IN (%s)" % ",".join("?" * len(oc.NOISY_EVENT_NAMES))
        args.extend(oc.NOISY_EVENT_NAMES)
    sql += " ORDER BY id DESC LIMIT ?"
    args.append(limit)
    rows = q(c, sql, tuple(args))
    types = q(c, "SELECT name, COUNT(*) n FROM events GROUP BY name ORDER BY n DESC")
    c.close()
    return {"anchored": ax.mode, "drift_s": ax.drift_s,
            "events": [{"t": ax(r["rt"], r["cap"]), "ring_ts": r["rt"], "tag": r["tag"], "name": r["name"],
                        "decoded": oc.jget(r["dj"])} for r in rows],
            "types": types, "hidden": [] if (type or all) else list(oc.NOISY_EVENT_NAMES)}


@app.get("/api/health")
def health():
    c = raw()
    ax = axis_for(c)
    ui = one(c, "SELECT decoded_json dj FROM events WHERE tag=? ORDER BY ring_timestamp DESC LIMIT 1",
             (oc.T_USER_INFO,))
    out = {
        "devices": q(c, "SELECT * FROM device"),
        "push": safe_q(c, "SELECT * FROM push_telemetry"),
        "axis": {"mode": ax.mode, "drift_s": ax.drift_s, "epochs": len(ax.epochs),
                 "first_anchor_ds": ax.first_anchor_ds},
        "user_info": oc.jget(ui["dj"]) if ui else None,
        "event_counts": q(c, "SELECT name, COUNT(*) n FROM events GROUP BY name ORDER BY n DESC"),
        "files": {}, "now": int(time.time()),
    }
    c.close()
    for p in (RAW_DB, DERIVED_DB):
        try:
            st = p.stat()
            out["files"][p.name] = {"size": st.st_size, "mtime": int(st.st_mtime)}
        except OSError:
            pass
    d = derived()
    if d:
        tabs = {r["name"] for r in q(d, "SELECT name FROM sqlite_master WHERE type='table'")}
        out["derived_counts"] = {t: one(d, f"SELECT COUNT(*) n FROM {t}")["n"] for t in sorted(tabs)
                                 if t != "sqlite_sequence"}
        out["profile"] = safe_one(d, "SELECT age_years, height_cm, weight_kg, sex, updated_unix FROM user_profile")
        d.close()
    return out


# ---------------------------------------------------------------- journal (tags)

@app.get("/api/tags")
def tags(days: int = Query(60, ge=1, le=730)):
    if not DERIVED_DB.exists():
        return {"tags": [], "kinds": TAG_KINDS}
    c = derived_rw()
    since = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    rows = q(c, "SELECT * FROM tags WHERE day>=? ORDER BY day DESC, created_unix DESC", (since,))
    c.close()
    return {"tags": rows, "kinds": TAG_KINDS}


@app.post("/api/tags")
def add_tag(body: dict):
    day = str(body.get("day") or "")
    kind = str(body.get("kind") or "")
    note = (body.get("note") or "").strip()[:280] or None
    try:
        datetime.strptime(day, "%Y-%m-%d")
    except ValueError as e:
        raise HTTPException(422, "date invalide (AAAA-MM-JJ)") from e
    if kind not in TAG_KINDS:
        raise HTTPException(422, "type de tag inconnu")
    c = derived_rw()
    cur = c.execute("INSERT INTO tags (day, kind, note, created_unix) VALUES (?,?,?,?)",
                    (day, kind, note, int(time.time())))
    c.commit()
    tid = cur.lastrowid
    c.close()
    return {"ok": True, "id": tid}


@app.delete("/api/tags/{tag_id}")
def del_tag(tag_id: int):
    c = derived_rw()
    n = c.execute("DELETE FROM tags WHERE id=?", (tag_id,)).rowcount
    c.commit()
    c.close()
    if not n:
        raise HTTPException(404, "tag introuvable")
    return {"ok": True}


# ---------------------------------------------------------------- profil

@app.post("/api/profile")
def set_profile(body: dict):
    def val(v, lo, hi, cast):
        if v is None or v == "":
            return None
        v = cast(v)
        if not lo <= v <= hi:
            raise HTTPException(422, f"valeur hors plage [{lo}, {hi}]")
        return v

    age = val(body.get("age_years"), 10, 120, int)
    height = val(body.get("height_cm"), 80, 230, int)
    weight = val(body.get("weight_kg"), 25, 300, float)
    sex = body.get("sex") or "unspecified"
    if sex not in ("male", "female", "unspecified"):
        raise HTTPException(422, "sexe invalide")
    if age is None and height is None and weight is None:
        raise HTTPException(422, "rien à enregistrer")
    c = derived_rw()
    c.execute("INSERT INTO user_profile VALUES (1, ?, ?, ?, ?, ?)"
              " ON CONFLICT(id) DO UPDATE SET age_years=excluded.age_years,"
              " height_cm=excluded.height_cm, weight_kg=excluded.weight_kg,"
              " sex=excluded.sex, updated_unix=excluded.updated_unix",
              (age, height, weight, sex, int(time.time())))
    c.commit()
    c.close()
    return {"ok": True}


# ---------------------------------------------------------------- assistant (LLM)

CHAT_SYSTEM = (
    "Tu es l'assistant d'analyse des données personnelles d'un utilisateur d'anneau Oura, "
    "collectées localement sans cloud. Tu reçois en JSON ses dernières nuits (durées, stades "
    "ESTIMÉS, FC la plus basse, FC moyenne, HRV moyen, Recovery Index, respiration expérimentale, "
    "température et écart à sa ligne de base), sa récupération (contributeurs, signes de tension), "
    "son activité récente (dont des pas ESTIMÉS non validés), sa dette de sommeil, ses références "
    "personnelles, son journal (tags), l'état de l'anneau et les dernières mesures avec leur ancienneté.\n"
    "Règles : réponds en français en vouvoyant, concis et factuel ; n'utilise QUE les chiffres du "
    "contexte (si une donnée manque, dis-le) ; pour les moyennes, minimums et maximums, reprends "
    "« statistiques_nuits » (ou l'outil statistiques) et ne calcule jamais toi-même ; tiens compte de "
    "« maintenant » (date ET heure) pour situer les mesures ; n'affirme aucun lien de cause ni aucun "
    "enchaînement (« après les journées actives… ») sans l'avoir vérifié nuit par nuit dans les "
    "données — sinon présente-le explicitement comme une hypothèse à vérifier ; "
    "les stades de sommeil sont des estimations peu fiables ; aucun diagnostic médical "
    "— ces mesures sont des estimations non validées cliniquement ; en cas de symptômes inquiétants, "
    "oriente vers un médecin (urgence : 15 ou 112)."
)


def _hmin(h):
    """Heures décimales → « 7 h 13 » (un modèle lit 7,22 h comme 7 h 22)."""
    if h is None:
        return None
    m = int(round(h * 60))
    return f"{m // 60} h {m % 60:02d}"


def _clock(unix):
    return datetime.fromtimestamp(unix).strftime("%d/%m %H:%M") if unix else None


def _age(unix):
    if not unix:
        return None
    m = int((time.time() - unix) // 60)
    ago = f"il y a {m} min" if m < 120 else f"il y a {m // 60} h {m % 60:02d}"
    return f"{ago} (à {_clock(unix)})"


CHAT_NIGHT_KEYS = (("night", "nuit"), ("score", "score_sommeil"), ("total", "sommeil_total"),
                   ("in_bed", "temps_au_lit"), ("deep", "profond_estime"), ("rem", "rem_estime"),
                   ("efficiency", "efficacite_pct"), ("latency", "latence_min"), ("waso", "eveil_nocturne_min"),
                   ("awakenings", "reveils_plus_de_5_min"), ("hr_min", "fc_la_plus_basse_bpm"),
                   ("hr_mean", "fc_moyenne_bpm"), ("hrv_rmssd", "hrv_moyen_ms"),
                   ("recovery_index", "indice_de_recuperation"), ("resp_rate", "respiration_par_min_experimental"),
                   ("temp_dev", "ecart_temperature_c"), ("ref_excluded", "exclue_des_references"))
CHAT_HOURS = ("total", "in_bed", "deep", "rem", "recovery_index")


def _night_ctx(r):
    """Ligne de sleep_scores → dictionnaire lisible par le modèle (durées en h/min)."""
    o = {name: (_hmin(r[k]) if k in CHAT_HOURS else r[k]) for k, name in CHAT_NIGHT_KEYS
         if r.get(k) is not None}
    o["coucher_lever"] = f"{r.get('bedtime')}–{r.get('wake_time')}"
    if r.get("provisional"):
        o["nuit_en_cours"] = True
    return o


JOURS_FR = ("lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche")

# (colonne, nom, format) des statistiques de nuits ; « h » = heures → « 7 h 40 »
STAT_NIGHT = (("score", "score_sommeil", None), ("total", "sommeil_total", "h"),
              ("efficiency", "efficacite_pct", None), ("latency", "latence_min", None),
              ("waso", "eveil_nocturne_min", None), ("hr_min", "fc_la_plus_basse_bpm", None),
              ("hr_mean", "fc_moyenne_bpm", None), ("hrv_rmssd", "hrv_moyen_ms", None),
              ("recovery_index", "indice_de_recuperation", "h"),
              ("resp_rate", "respiration_par_min", None), ("temp_dev", "ecart_temperature_c", None))
STAT_ACT = (("met_min_mh", "effort_modere_intense_met_min", None), ("steps", "pas_estimes_non_valides", None),
            ("inactive_min", "temps_inactif", "min"), ("total_kcal", "kcal_totales_estimees", None))


def _stat(rows, key, fmt, label="night"):
    """Moyenne, minimum et maximum (avec leur date) d'une colonne : calculés ici,
    jamais par le modèle (il se trompe sur 30 valeurs)."""
    vals = [(r[label], r[key]) for r in rows if r.get(key) is not None]
    if not vals:
        return None
    lo, hi = min(vals, key=lambda x: x[1]), max(vals, key=lambda x: x[1])
    mean = sum(v for _, v in vals) / len(vals)
    f = (lambda v: _hmin(v)) if fmt == "h" else (lambda v: _hmin(v / 60)) if fmt == "min" else (
        lambda v: round(v, 2 if key == "temp_dev" else 1))
    return {"moyenne": f(mean), "min": f(lo[1]), "date_min": lo[0], "max": f(hi[1]), "date_max": hi[0],
            "n": len(vals)}


def _clock_stat(rows, f):
    """Statistiques d'une heure de la nuit (coucher, lever, milieu du sommeil) :
    minutes depuis 18:00 pour que 23:30 et 01:30 se comparent, écart-type compris
    (la régularité des horaires est un facteur majeur de la qualité du sommeil)."""
    vals = []
    for r in rows:
        u = f(r)
        if u:
            t = datetime.fromtimestamp(u)
            vals.append((r["night"], (t.hour * 60 + t.minute - 18 * 60) % 1440))
    if len(vals) < 2:
        return None
    hm = lambda m: f"{int((m + 18 * 60) % 1440) // 60:02d}:{int(m) % 60:02d}"  # noqa: E731
    mean = sum(v for _, v in vals) / len(vals)
    sd = (sum((v - mean) ** 2 for _, v in vals) / (len(vals) - 1)) ** 0.5
    lo, hi = min(vals, key=lambda x: x[1]), max(vals, key=lambda x: x[1])
    return {"moyenne": hm(mean), "ecart_type_min": round(sd), "plus_tot": hm(lo[1]), "date_plus_tot": lo[0],
            "plus_tard": hm(hi[1]), "date_plus_tard": hi[0], "amplitude": _hmin((hi[1] - lo[1]) / 60),
            "n": len(vals)}


def night_stats(rows):
    out = {name: _stat(rows, k, fmt) for k, name, fmt in STAT_NIGHT}
    out["heure_coucher"] = _clock_stat(rows, lambda r: r.get("start_unix"))
    out["heure_lever"] = _clock_stat(rows, lambda r: r.get("stop_unix"))
    out["milieu_du_sommeil"] = _clock_stat(
        rows, lambda r: (r["onset_unix"] + r["end_unix"]) / 2 if r.get("onset_unix") and r.get("end_unix") else None)
    return {k: v for k, v in out.items() if v}


def activity_stats(rows):
    full = [r for r in rows if not r.get("partial")]
    out = {name: _stat(full, k, fmt, "day") for k, name, fmt in STAT_ACT}
    return {k: v for k, v in out.items() if v}


def _now_label():
    n = datetime.now()
    return f"{JOURS_FR[n.weekday()]} {n.strftime('%d/%m/%Y %H:%M')}"


def chat_context():
    ctx = {"maintenant": _now_label(),
           "convention": "une nuit porte la date du RÉVEIL (nuit du 28/10 = coucher le 27 au soir, réveil le "
                         "28) ; le journal du jour J concerne la nuit J+1 ; journée d'activité = 04:00 → 03:59"}
    d = derived()
    if d:
        nights_ = []
        rows30 = list(reversed(safe_q(d, "SELECT * FROM sleep_scores ORDER BY night DESC LIMIT 30")))
        for r in rows30:
            nights_.append(_night_ctx(r))
        ctx["nuits"] = nights_
        full = [r for r in rows30 if not r.get("provisional")]
        ctx["statistiques_nuits"] = {
            "periode": f"{full[0]['night']} → {full[-1]['night']} ({len(full)} nuits)" if full else None,
            **night_stats(full)}
        # durées en « 7 h 30 » : 7,5 h a été lu « 7 h 5 » (28/10/2026)
        ctx["references_personnelles"] = {
            m: ({"moyenne": _hmin(b["mean"]), "ecart_type": _hmin(b["sd"]), "nuits": b["n"]} if m == "total" else
                {"moyenne": round(b["mean"], 1), "ecart_type": round(b["sd"], 1), "nuits": b["n"]})
            for m, b in baselines(d).items() if m in ("hr_min", "hr_mean", "hrv_rmssd", "resp_rate", "total")}
        rd = readiness_row(safe_one(d, "SELECT * FROM readiness ORDER BY day DESC LIMIT 1"))
        if rd:
            ctx["recuperation"] = {"jour": rd["day"], "score": rd["score"],
                                   "contributeurs_sur_100": {k: v.get("score") for k, v in rd["contributors"].items()},
                                   "signes_de_tension": rd["tension"].get("level"),
                                   "dette_de_sommeil_14j": _hmin(rd["sleep_debt_h"]),
                                   "besoin_de_sommeil_retenu": _hmin(rd["sleep_need_h"])}
        ctx["activite_7j"] = [
            {"jour": a["day"], "journee_incomplete": bool(a["partial"]), "score": a.get("score"),
             "pas_estimes_non_valides": a.get("steps"), "effort_modere_intense_met_min": a["met_min_mh"],
             "temps_inactif": _hmin(a["inactive_min"] / 60), "kcal_totales_estimees": a.get("total_kcal"),
             "fc_repos_de_jour_bpm": a.get("hr_day_rest")}
            for a in safe_q(d, "SELECT * FROM activity_daily ORDER BY day DESC LIMIT 7")]
        ctx["statistiques_activite_7j_completes"] = activity_stats(
            safe_q(d, "SELECT * FROM activity_daily ORDER BY day DESC LIMIT 7"))
        ctx["repos_7j"] = [{"debut": _clock(x["start_unix"]), "minutes": x["minutes"], "type": x["kind"]}
                           for x in safe_q(d, "SELECT * FROM rests WHERE start_unix>? ORDER BY start_unix",
                                           (time.time() - 7 * 86400,))]
        ctx["journal"] = safe_q(d, "SELECT day, kind, note FROM tags ORDER BY day DESC LIMIT 20")
        d.close()
    try:
        c = raw()
        ax = axis_for(c)
        st = ring_status(c, ax)
        live = live_signals(c, ax)
        c.close()
        bat = st.get("battery") or {}
        rem = bat.get("remaining_h")
        ctx["anneau"] = {"batterie_pct": bat.get("pct"),
                         "autonomie_restante": f"{rem / 24:.1f} jours".replace(".", ",") if rem else None,
                         "etat": (st.get("state") or {}).get("label"), "porte": st.get("worn")}
        ctx["dernieres_mesures"] = {
            "fc_de_jour_bpm": {"valeur": live["hr"]["v"], "quand": _age(live["hr"]["t"])} if live.get("hr") else None,
            "intensite_met": {"valeur": live["met"]["v"], "quand": _age(live["met"]["t"])} if live.get("met") else None,
            "temperature_cutanee_c": ({"valeur": st["skin_temp"]["v"], "quand": _age(st["skin_temp"]["t"])}
                                      if st.get("skin_temp") else None)}
    except HTTPException:
        pass
    return json.dumps(ctx, ensure_ascii=False, default=str)


def llm_options(model):
    """Champs de requête du gros modèle : Ling (bailing v3) n'a que
    enable_thinking, borné par un budget de réflexion (sinon ~5 min) ;
    Qwen 3.8 a des niveaux d'effort."""
    if "ling" in model.lower():
        return {"chat_template_kwargs": {"enable_thinking": True},
                "thinking_budget_tokens": int(os.environ.get("OURA_LLM_THINK_BUDGET", "2000")),
                "reasoning_budget_message": "Okay, I have thought enough. Now I will write the final answer in French."}
    return {"chat_template_kwargs": {"reasoning_effort": "medium"}}


async def swap_running():
    try:
        async with httpx.AsyncClient(timeout=5) as cli:
            r = await cli.get(LLM_URL.split("/v1/")[0] + "/running")
            return [m.get("model") for m in r.json().get("running", []) if m.get("state") == "ready"]
    except Exception:  # noqa: BLE001
        return None


# ---------------------------------------------------------------- outils de l'analyse approfondie
#
# Le modèle de l'analyse approfondie (Ling-Santé) reçoit le même contexte résumé que
# le chat rapide, plus des OUTILS pour aller chercher le détail calculé par le code
# (courbes 5 min, hypnogramme, minute par minute…). Il ne lit jamais le brut de
# l'anneau : volume (~26 000 événements/nuit) et décodage piégeux (TimeAxis,
# décisecondes, 3 capteurs de T°) — le code calcule, le modèle interprète.

TOOL_MAX_CHARS = 24000        # ~7 000 jetons par réponse d'outil (contexte 64K)
CHAT_TOOL_ROUNDS = 5          # allers-retours d'outils maximum avant la réponse
# Réflexion de Ling (~44 jetons/s) : 4 000 par aller-retour d'outils, 6 000 pour la
# réponse finale. À 1 200 (premier réglage), une demande « réfléchis profondément »
# recopiait les statistiques sans rien vérifier (28/10/2026).
CHAT_ROUND_BUDGET = int(os.environ.get("OURA_CHAT_THINK_ROUND", "4000"))
CHAT_FINAL_BUDGET = int(os.environ.get("OURA_CHAT_THINK_FINAL", "6000"))
CHAT_DEEP_DEADLINE_S = 720    # au-delà : réponse forcée avec ce qui a été lu
CHAT_FINAL_RESERVE_S = 240    # temps gardé pour la réponse forcée (6 000 jetons ≈ 2 min 20)
ZOOM_MAX_H = 4


def _hm(unix):
    return datetime.fromtimestamp(unix).strftime("%H:%M") if unix else None


def _rnd(v, nd=0):
    if v is None:
        return None
    v = round(float(v), nd)
    return int(v) if nd == 0 else v


def _parse_day(s):
    datetime.strptime(str(s), "%Y-%m-%d")
    return str(s)


def _parse_dt(s):
    s = str(s).strip().replace("T", " ")
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(s, fmt).timestamp()
        except ValueError:
            pass
    raise ValueError(f"date-heure invalide : {s} (attendu AAAA-MM-JJ HH:MM)")


def tool_nuits(debut, fin):
    """Résumés de nuits sur une période (au-delà des 30 du contexte)."""
    debut, fin = _parse_day(debut), _parse_day(fin)
    d = derived()
    if not d:
        return {"erreur": "aucune donnée dérivée"}
    rows = safe_q(d, "SELECT * FROM sleep_scores WHERE night BETWEEN ? AND ? ORDER BY night LIMIT 120",
                  (debut, fin))
    d.close()
    return {"nuits": [_night_ctx(r) for r in rows], "n": len(rows)}


def tool_courbes_nuit(date):
    """Courbes 5 min + hypnogramme estimé (segments) d'une nuit."""
    date = _parse_day(date)
    d = derived()
    if not d:
        return {"erreur": "aucune donnée dérivée"}
    n = safe_one(d, "SELECT * FROM sleep_scores WHERE night=?", (date,))
    if not n:
        d.close()
        return {"erreur": f"nuit {date} inconnue (une nuit porte la date du réveil)"}
    series = safe_q(d, "SELECT t, hr, rmssd, resp, motion, temp FROM night_series WHERE night=? ORDER BY t", (date,))
    stages = [r["stage"] for r in safe_q(d, "SELECT stage FROM night_staging WHERE night=? ORDER BY epoch", (date,))]
    d.close()
    names = {"awake": "éveil", "light": "léger", "deep": "profond", "rem": "REM"}
    runs, t0 = [], n.get("start_unix")
    if t0:
        i = 0
        while i < len(stages):
            j = i
            while j + 1 < len(stages) and stages[j + 1] == stages[i]:
                j += 1
            runs.append([_hm(t0 + i * 30), _hm(t0 + (j + 1) * 30), names.get(stages[i], stages[i])])
            i = j + 1
    return {
        "nuit": date, "resume": _night_ctx(n),
        "coucher": _clock(n.get("start_unix")), "endormissement": _clock(n.get("onset_unix")),
        "fin_du_sommeil": _clock(n.get("end_unix")), "lever": _clock(n.get("stop_unix")),
        "fc_stabilisee_a": _clock(n.get("hr_stable_at")),
        "colonnes": ["heure", "fc_bpm", "hrv_rmssd_ms", "respiration_par_min", "mouvement_max_sans_unite",
                     "temperature_peau_c"],
        "points_5_min": [[_hm(r["t"]), _rnd(r["hr"]), _rnd(r["rmssd"]), _rnd(r["resp"], 1),
                          _rnd(r["motion"], 2), _rnd(r["temp"], 2)] for r in series],
        "hypnogramme_estime": runs,
        "note": "stades = estimation heuristique peu fiable ; mouvement = intensité relative (0 = immobile, pics > 10 = gros mouvements) ; valeurs absentes = null",
    }


def tool_recuperation(date):
    """Score de récupération d'un jour : contributeurs et signes de tension détaillés."""
    date = _parse_day(date)
    d = derived()
    if not d:
        return {"erreur": "aucune donnée dérivée"}
    rd = readiness_row(safe_one(d, "SELECT * FROM readiness WHERE day=?", (date,)))
    d.close()
    if not rd:
        return {"erreur": f"pas de récupération pour {date}"}
    rd["contributors"] = {k: v.get("score") if isinstance(v, dict) else v for k, v in rd["contributors"].items()}
    rd["sleep_debt"] = _hmin(rd.pop("sleep_debt_h", None))
    rd["sleep_need"] = _hmin(rd.pop("sleep_need_h", None))
    return rd


def tool_activite_jour(date):
    """Journée d'activité (04:00 → 03:59) agrégée par quart d'heure."""
    date = _parse_day(date)
    a = activity(date=date)
    q15 = {}
    for p in a["met"]:
        b = q15.setdefault(int(p["t"] // 900) * 900, {"met": [], "pas": 0.0, "fc": []})
        b["met"].append(p["met"])
        b["pas"] += p["steps"]
    for p in a["hr"]:
        if not p.get("night"):
            q15.setdefault(int(p["t"] // 900) * 900, {"met": [], "pas": 0.0, "fc": []})["fc"].append(p["hr"])
    rows = []
    for k in sorted(q15):
        b = q15[k]
        fc = sorted(b["fc"])
        rows.append([_hm(k), _rnd(sum(b["met"]) / len(b["met"]), 1) if b["met"] else None,
                     _rnd(b["pas"]), fc[len(fc) // 2] if fc else None])
    s = a.get("summary") or {}
    return {
        "jour": date, "journee": "04:00 → 03:59",
        "resume": {k: s.get(k) for k in ("score", "partial", "steps", "met_min_mh", "inactive_min",
                                          "active_kcal", "total_kcal", "hr_day_rest", "hr_day_mean") if k in s},
        "colonnes": ["quart_d_heure", "met_moyen", "pas_estimes", "fc_mediane_bpm"],
        "par_quart_d_heure": rows,
        "efforts_met_3_et_plus": [{"debut": _hm(x["start"]), "fin": _hm(x["end"]), "minutes": x["min"],
                                   "met_moyen": x["met_avg"], "fc_moyenne": x["hr_mean"], "fc_max": x["hr_max"]}
                                  for x in a["segments"]],
        "sommeil": [[_hm(x), _hm(y)] for x, y in a["sleep"]],
        "repos": [{"debut": _hm(x["start_unix"]), "fin": _hm(x["end_unix"]), "type": x["kind"],
                   "fc_mediane": x["hr_median"]} for x in a["rest_list"]],
        "charge": [[_hm(x), _hm(y)] for x, y in a["charging"]],
        "non_porte": a["nonworn"],
        "note": "pas = estimation non validée ; MET : < 1,05 repos, 1,05–1,5 sédentaire, ≥ 3 modéré, ≥ 6 intense",
    }


def tool_zoom(debut, fin):
    """Minute par minute (FC de jour, MET, pas) sur une fenêtre de ≤ 4 h."""
    t0, t1 = _parse_dt(debut), _parse_dt(fin)
    if not 0 < t1 - t0 <= ZOOM_MAX_H * 3600:
        return {"erreur": f"fenêtre invalide : fin après début, {ZOOM_MAX_H} h maximum"}
    c = raw()
    ax = axis_for(c)
    hr = oc.day_hr_minutes(c, ax, t0, t1)
    am = oc.activity_minutes(c, ax, t0, t1)
    s_ds, e_ds = ax.to_ds(t0 - 600), ax.to_ds(t1 + 600)
    night = [[_hm(ax(cds)), h, x] for cds, h, x in oc.hrv_bins(c, s_ds, e_ds)
             if ax(cds) is not None and t0 <= ax(cds) < t1]
    c.close()
    mins = sorted(set(int(t // 60) * 60 for t in list(hr) + list(am)))
    hr_m = {int(t // 60) * 60: v for t, v in hr.items()}
    am_m = {int(t // 60) * 60: v for t, v in am.items()}
    return {
        "debut": _clock(t0), "fin": _clock(t1),
        "colonnes": ["minute", "fc_bpm", "met", "pas_estimes"],
        "par_minute": [[_hm(m), _rnd(hr_m.get(m)), _rnd((am_m.get(m) or {}).get("met"), 1),
                        _rnd((am_m.get(m) or {}).get("steps"))] for m in mins],
        "fc_de_nuit_5_min": {"colonnes": ["fin_de_fenetre", "fc_bpm", "hrv_rmssd_ms"], "points": night},
        "note": "FC de jour mesurée par intermittence (minutes sans mesure = null) ; la nuit, FC/HRV par fenêtres de 5 min",
    }


def tool_statistiques(debut, fin):
    """Moyenne / min / max (avec dates) des nuits, de la récupération et de l'activité sur une période."""
    debut, fin = _parse_day(debut), _parse_day(fin)
    d = derived()
    if not d:
        return {"erreur": "aucune donnée dérivée"}
    nights_ = [r for r in safe_q(d, "SELECT * FROM sleep_scores WHERE night BETWEEN ? AND ? ORDER BY night",
                                 (debut, fin)) if not r.get("provisional")]
    rd = safe_q(d, "SELECT day, score FROM readiness WHERE day BETWEEN ? AND ? ORDER BY day", (debut, fin))
    act = safe_q(d, "SELECT * FROM activity_daily WHERE day BETWEEN ? AND ? ORDER BY day", (debut, fin))
    d.close()
    return {"periode": f"{debut} → {fin}", "nuits": len(nights_), "sommeil": night_stats(nights_),
            "score_recuperation": _stat(rd, "score", None, "day"),
            "activite_jours_complets": activity_stats(act),
            "note": "valeurs calculées par le code : les reprendre telles quelles"}


def tool_journal(debut, fin):
    """Tags du journal sur une période (maladie, alcool, sport, stress…)."""
    debut, fin = _parse_day(debut), _parse_day(fin)
    d = derived()
    if not d:
        return {"journal": []}
    rows = safe_q(d, "SELECT day, kind, note FROM tags WHERE day BETWEEN ? AND ? ORDER BY day", (debut, fin))
    d.close()
    return {"journal": rows}


def _fn(name, desc, props, required):
    return {"type": "function", "function": {"name": name, "description": desc, "parameters": {
        "type": "object", "properties": {k: {"type": "string", "description": v} for k, v in props.items()},
        "required": required}}}


_DAY = "date AAAA-MM-JJ"
CHAT_TOOLS = [
    _fn("courbes_nuit", "Courbes toutes les 5 min d'une nuit (FC, HRV, respiration, mouvement, température "
        "de peau), heures de coucher/endormissement/réveil et hypnogramme estimé. Une nuit porte la date du RÉVEIL.",
        {"date": _DAY}, ["date"]),
    _fn("nuits", "Résumés de toutes les nuits entre deux dates (pour remonter au-delà des 30 nuits du contexte).",
        {"debut": _DAY, "fin": _DAY}, ["debut", "fin"]),
    _fn("recuperation", "Score de récupération d'un jour : contributeurs et signes de tension détaillés "
        "(écarts aux références).", {"date": _DAY}, ["date"]),
    _fn("activite_jour", "Journée d'activité (04:00 → 03:59) par quart d'heure : MET, pas estimés, FC de jour ; "
        "efforts, repos, sommeil, charge, périodes non portées.", {"date": _DAY}, ["date"]),
    _fn("zoom", f"Minute par minute (FC de jour, MET, pas) et FC/HRV de nuit par 5 min, sur une fenêtre "
        f"de {ZOOM_MAX_H} h maximum.", {"debut": "AAAA-MM-JJ HH:MM", "fin": "AAAA-MM-JJ HH:MM"}, ["debut", "fin"]),
    _fn("statistiques", "Moyenne, minimum et maximum (avec leur date) du sommeil, de la récupération et de "
        "l'activité sur une période quelconque. À utiliser pour toute moyenne ou comparaison de périodes.",
        {"debut": _DAY, "fin": _DAY}, ["debut", "fin"]),
    _fn("journal", "Tags du journal de l'utilisateur entre deux dates (maladie, alcool, sport, stress…).",
        {"debut": _DAY, "fin": _DAY}, ["debut", "fin"]),
]
TOOL_FUNCS = {"courbes_nuit": tool_courbes_nuit, "nuits": tool_nuits, "recuperation": tool_recuperation,
              "activite_jour": tool_activite_jour, "zoom": tool_zoom, "journal": tool_journal,
              "statistiques": tool_statistiques}

CHAT_TOOLS_HELP = (
    "\nOutils : le contexte ci-dessous ne contient que des résumés. Pour toute question qui demande "
    "du détail (heure d'un minimum ou d'un pic, évolution pendant la nuit, réveils, journée précise, "
    "nuit ancienne, comparaison fine), appelle d'abord les outils adaptés, puis réponds en t'appuyant "
    "sur leurs valeurs (cite les heures). Avant d'affirmer un enchaînement ou une tendance (« après "
    "les journées actives, la HRV baisse »), vérifie-le avec nuits / activite_jour / statistiques. "
    "Pour une demande d'analyse (qualité, tendance, explication, « réfléchis »), ne te contente pas de "
    "recopier les statistiques : identifie 2 ou 3 hypothèses, VÉRIFIE-LES avec les outils (par exemple "
    "courbes_nuit des nuits extrêmes, nuits ou statistiques sur des sous-périodes, activite_jour la veille), "
    "puis dis lesquelles tiennent et lesquelles ne tiennent pas. "
    "N'invente aucune valeur qu'un outil n'a pas renvoyée. "
    f"Aujourd'hui : {{today}}."
)


def run_tool(name, args):
    """Exécute un outil et renvoie du JSON compact (erreurs comprises, le modèle les lit)."""
    f = TOOL_FUNCS.get(name)
    if not f:
        out = {"erreur": f"outil inconnu : {name}"}
    else:
        try:
            out = f(**{k: v for k, v in (args or {}).items() if isinstance(k, str)})
        except HTTPException as e:
            out = {"erreur": e.detail}
        except (TypeError, ValueError) as e:
            out = {"erreur": f"arguments invalides : {e}"}
    s = json.dumps(out, ensure_ascii=False, default=str, separators=(",", ":"))
    if len(s) > TOOL_MAX_CHARS:
        s = s[:TOOL_MAX_CHARS] + "…(tronqué : demander une période plus courte)"
    return s


def _tool_label(name, args):
    return f"{name}({', '.join(str(v) for v in (args or {}).values())})"


CHAT_JOBS: dict[str, dict] = {}


async def deep_chat_job(job, msg):
    """Analyse approfondie : boucle modèle ↔ outils, puis réponse finale."""
    system = (CHAT_SYSTEM + CHAT_TOOLS_HELP.format(today=datetime.now().strftime("%Y-%m-%d"))
              + "\nContexte :\n" + await asyncio.to_thread(chat_context))
    messages = [{"role": "system", "content": system}, {"role": "user", "content": msg}]
    deadline = time.monotonic() + CHAT_DEEP_DEADLINE_S
    try:
        async with httpx.AsyncClient(timeout=300) as cli:
            for rnd in range(CHAT_TOOL_ROUNDS + 1):
                final = rnd == CHAT_TOOL_ROUNDS or time.monotonic() > deadline - CHAT_FINAL_RESERVE_S
                budget = CHAT_FINAL_BUDGET if final else CHAT_ROUND_BUDGET
                payload = {"model": LLM_MODEL_DEEP, "messages": messages, "temperature": 0.4,
                           "max_tokens": budget + 4000, **llm_options(LLM_MODEL_DEEP)}
                if "thinking_budget_tokens" in payload:
                    payload["thinking_budget_tokens"] = budget
                if not final:
                    payload["tools"] = CHAT_TOOLS
                job["step"] = "rédaction de la réponse" if final else (
                    "chargement du modèle et réflexion" if rnd == 0 else "réflexion")
                r = await cli.post(LLM_URL, json=payload)
                r.raise_for_status()
                m = r.json()["choices"][0]["message"]
                calls = m.get("tool_calls") or []
                if calls and not final:
                    # raisonnement NON renvoyé : 5 tours x 4 000 jetons + réponses d'outils
                    # dépasseraient le contexte de 64K du modèle
                    messages.append({"role": "assistant", "content": m.get("content") or "", "tool_calls": calls})
                    for c in calls:
                        name = (c.get("function") or {}).get("name", "")
                        try:
                            args = json.loads((c.get("function") or {}).get("arguments") or "{}")
                        except ValueError:
                            args = {}
                        job["tools"].append(_tool_label(name, args))
                        job["step"] = "lecture : " + job["tools"][-1]
                        out = await asyncio.to_thread(run_tool, name, args)
                        messages.append({"role": "tool", "tool_call_id": c.get("id") or name, "content": out})
                    continue
                content = (m.get("content") or "").strip()
                if not content:
                    raise RuntimeError("réponse vide du modèle")
                job.update(state="done", reply=content, step=None)
                return
    except Exception as e:  # noqa: BLE001
        job.update(state="error", error=f"analyse approfondie impossible : {e}", step=None)


def _purge_jobs():
    old = time.time() - 3600
    for k in [k for k, j in CHAT_JOBS.items() if j["created"] < old]:
        CHAT_JOBS.pop(k, None)


@app.get("/api/chat/job/{job_id}")
def chat_job(job_id: str):
    j = CHAT_JOBS.get(job_id)
    if not j:
        raise HTTPException(404, "analyse inconnue ou expirée")
    return {k: j.get(k) for k in ("state", "step", "tools", "reply", "error", "model", "created")}


@app.post("/api/chat")
async def chat(body: dict):
    msg = (body.get("message") or "").strip()
    deep = bool(body.get("deep"))
    if not msg:
        raise HTTPException(400, "message vide")
    if deep and not LLM_MAY_SWAP:
        # la 3090 ne porte qu'un modèle à la fois : ne jamais décharger un
        # modèle utilisé ailleurs pour une question du portail
        run = await swap_running()
        busy = [m for m in (run or []) if m not in (LLM_MODEL_DEEP, LLM_MODEL)]
        if busy:
            raise HTTPException(409, f"la RTX 3090 est occupée par {busy[0]} : réessayez plus tard "
                                     "ou utilisez « Envoyer » (modèle rapide)")
    if deep:
        # plusieurs allers-retours modèle ↔ outils : plus long que le délai de
        # nginx (300 s) → tâche de fond, la page interroge /api/chat/job/{id}
        _purge_jobs()
        jid = secrets.token_urlsafe(9)
        job = CHAT_JOBS[jid] = {"state": "running", "step": "démarrage", "tools": [], "reply": None,
                                "error": None, "model": LLM_MODEL_DEEP, "created": time.time()}
        job["task"] = asyncio.create_task(deep_chat_job(job, msg))
        return {"job": jid, "model": LLM_MODEL_DEEP}
    payload = {
        "model": LLM_MODEL,
        "messages": [{"role": "system", "content": CHAT_SYSTEM + "\nContexte :\n" + chat_context()},
                     {"role": "user", "content": msg}],
        "temperature": 0.4,
        # réflexion coupée pour le modèle rapide (sinon elle consomme tout le budget)
        "max_tokens": 800,
        "chat_template_kwargs": {"enable_thinking": False},
    }
    try:
        async with httpx.AsyncClient(timeout=280) as cli:
            r = await cli.post(LLM_URL, json=payload)
            r.raise_for_status()
            content = (r.json()["choices"][0]["message"].get("content") or "").strip()
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"llama-swap indisponible : {e}") from e
    if not content:
        raise HTTPException(502, "llama-swap a renvoyé une réponse vide")
    return {"reply": content, "model": payload["model"]}


# ---------------------------------------------------------------- ingestion (téléphone)

@app.post("/ingest/events")
async def ingest_events(request: Request):
    """Envoi d'événements par une source (téléphone).

    Payload : {serial, cursor, pushed_unix, source?, events:[{tag, ring_timestamp,
    body(base64) | body_hex, decoded_json?, captured_unix?}]}
    Dédup sur UNIQUE(serial, tag, ring_timestamp, body) ; push_telemetry
    alimente l'indicateur de fraîcheur du portail.
    """
    if not INGEST_TOKENS:
        raise HTTPException(
            503, "aucun jeton d'ingestion configuré (OURA_PHONE_TOKEN absent — voir "
                 "/etc/oura/ingest.env)")
    token = request.headers.get("x-oura-token", "")
    src = next((s for s, t in INGEST_TOKENS.items() if hmac.compare_digest(t, token)), None)
    if src is None:
        raise HTTPException(401, "X-Oura-Token invalide")
    p = await request.json()
    serial = str(p.get("serial") or "")
    events_in = p.get("events") or []
    cursor = int(p.get("cursor") or 0)
    now = int(time.time())
    c = sqlite3.connect(str(RAW_DB), timeout=15)
    c.execute("PRAGMA busy_timeout=15000")
    c.execute("""CREATE TABLE IF NOT EXISTS push_telemetry (
        source TEXT PRIMARY KEY, last_push_unix INTEGER, cursor INTEGER,
        events_pushed INTEGER, last_status TEXT)""")
    try:
        inserted = 0
        for e in events_in:
            tag = int(e.get("tag", -1))
            name = e.get("name") or TAG_NAMES.get(tag, f"tag_{tag}")
            body_b = e.get("body")
            if isinstance(body_b, str):
                body_b = base64.b64decode(body_b)
            elif body_b is None and (bh := e.get("body_hex")):
                body_b = bytes.fromhex(bh)  # format du core Rust (recent_events)
            else:
                body_b = bytes(body_b or b"")
            dj = e.get("decoded_json")
            if isinstance(dj, (dict, list)):  # le core Rust renvoie un objet JSON
                dj = json.dumps(dj, separators=(",", ":"))
            r = c.execute(
                "INSERT OR IGNORE INTO events "
                "(serial, tag, name, ring_timestamp, body, decoded_json, captured_unix) "
                "VALUES (?,?,?,?,?,?,?)",
                (serial, tag, name, int(e.get("ring_timestamp", 0)), body_b,
                 dj, int(e.get("captured_unix") or now)))
            inserted += r.rowcount
        c.execute(
            "INSERT INTO push_telemetry (source, last_push_unix, cursor, events_pushed, last_status) "
            "VALUES (?,?,?,?,?) "
            "ON CONFLICT(source) DO UPDATE SET "
            "last_push_unix=excluded.last_push_unix, cursor=excluded.cursor, "
            "events_pushed=excluded.events_pushed, last_status=excluded.last_status",
            (src, now, cursor, len(events_in), "ok"))
        c.commit()
        return {"inserted": inserted, "received": len(events_in), "source": src}
    finally:
        c.close()


# ---------------------------------------------------------------- interface

def _asset_version():
    h = hashlib.sha1()
    for f in ("app.js", "app.css"):
        try:
            h.update((STATIC_DIR / f).read_bytes())
        except OSError:
            pass
    return h.hexdigest()[:10]


if STATIC_DIR.is_dir():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/", response_class=HTMLResponse)
def index():
    try:
        html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    except OSError as e:
        raise HTTPException(500, "interface absente (static/index.html)") from e
    return HTMLResponse(html.replace("{{v}}", _asset_version()),
                        headers={"Cache-Control": "no-cache"})
