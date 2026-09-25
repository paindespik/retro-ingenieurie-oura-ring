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
import base64
import hashlib
from contextlib import asynccontextmanager
import hmac
import json
import os
import sqlite3
import time
from datetime import datetime, timedelta
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

import oura_core as oc

RAW_DB = Path(os.environ.get("OURA_RAW_DB", "/srv/oura/oura.db"))
DERIVED_DB = Path(os.environ.get("OURA_DERIVED_DB", "/srv/oura/derived.db"))
INBOX_DIR = Path(os.environ.get("OURA_INBOX", "/srv/oura/inbox"))
STATIC_DIR = Path(__file__).with_name("static")
LLM_URL = os.environ.get("OURA_LLM_URL", "http://127.0.0.1:8012/v1/chat/completions")
LLM_MODEL = os.environ.get("OURA_LLM_MODEL", "qwen3.5-4b")
LLM_MODEL_DEEP = os.environ.get("OURA_LLM_MODEL_DEEP", "qwen3.8-27b-gsq")
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
    return r


def baselines(d):
    return {b["metric"]: b for b in safe_q(d, "SELECT * FROM baselines")} if d else {}


def readiness_row(r):
    if not r:
        return None
    return {"day": r["day"], "score": r["score"], "rating": oc.rating(r["score"]),
            "contributors": jparse(r["contributors"], {}), "tension": jparse(r["tension"], {}),
            "n_base": r["n_base"], "provisional": bool(r["provisional"])}


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


def ring_status(c, ax):
    dev = one(c, "SELECT serial, firmware, api_version FROM device ORDER BY updated_unix DESC LIMIT 1")
    bat = latest_battery(c)
    if bat:
        bat["t"] = ax(bat.pop("rt"), bat.pop("cap"))
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
        out["baselines"] = baselines(d)
        if n:
            out["staging"] = staging_payload(d, n)
            out["readiness"] = readiness_row(safe_one(d, "SELECT * FROM readiness WHERE day=?", (n["night"],)))
        today = datetime.now().strftime("%Y-%m-%d")
        out["activity"] = activity_row(safe_one(d, "SELECT * FROM activity_daily WHERE day=?", (today,)))
        yday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
        out["activity_yesterday"] = activity_row(safe_one(d, "SELECT * FROM activity_daily WHERE day=?", (yday,)))
        b = safe_one(d, "SELECT * FROM llm_briefings ORDER BY ts DESC LIMIT 1")
        if b:
            b["stale"] = bool(n and (b["night"] != n["night"] or (n.get("ts") or 0) > b["ts"]))
        out["briefing"] = b
        d.close()
    return out


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
        "baselines": baselines(d),
        "readiness": readiness_row(safe_one(d, "SELECT * FROM readiness WHERE day=?", (date,))),
        "briefing": safe_one(d, "SELECT * FROM llm_briefings WHERE night=? ORDER BY ts DESC LIMIT 1", (date,)),
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
                     " temp_status, restfulness FROM sleep_scores ORDER BY night DESC LIMIT ?", (days,))
    rd = {r["day"]: r["score"] for r in safe_q(d, "SELECT day, score FROM readiness")}
    act = safe_q(d, "SELECT day, met_min_mh, inactive_min, active_kcal, score, partial FROM activity_daily"
                    " ORDER BY day DESC LIMIT ?", (days,))
    for r in rows:
        r["readiness"] = rd.get(r["night"])
    out = {"nights": list(reversed(rows)), "activity": list(reversed(act)), "baselines": baselines(d)}
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
    """Journée civile : bins MET 1 min, FC, tranches d'effort, sommeil, charge."""
    c = raw()
    ax = axis_for(c)
    d0 = datetime.strptime(date, "%Y-%m-%d") if date else datetime.now().replace(
        hour=0, minute=0, second=0, microsecond=0)
    t0 = d0.timestamp()
    t1 = t0 + 86400
    day = d0.strftime("%Y-%m-%d")
    mets = oc.met_minutes(c, ax, t0, t1)
    met_list = [{"t": t, "met": v} for t, v in sorted(mets.items())]

    # FC diurne : mesures de 1 min de l'anneau (tag 128, battements de bonne qualité)
    hr = []
    s_ds, e_ds = ax.to_ds(t0 - 600), ax.to_ds(t1 + 600)
    for r in q(c, "SELECT ring_timestamp rt, captured_unix cap, decoded_json dj FROM events"
                  " WHERE tag=? AND ring_timestamp>=? AND ring_timestamp<?", (oc.T_GREEN_IBI, s_ds, e_ds)):
        j = oc.jget(r["dj"]) or {}
        beats = sorted(b for b in (j.get("hr_bpm") or []) if oc.HR_MIN_BPM <= b <= oc.HR_MAX_BPM)
        if len(beats) >= 5:
            t = ax(r["rt"], r["cap"])
            if t is not None and t0 <= t < t1:
                hr.append({"t": t, "hr": beats[len(beats) // 2]})
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
    rests = [(a, b) for a, b in oc.short_rest_periods(c, ax)
             if b > t0 and a < t1 and not any(a < y and b > x for x, y in sleep)]
    charging = []
    for a, b in oc.charging_intervals_ds(c):
        ta, tb = ax(a), ax(b)
        if ta and tb and tb > t0 and ta < t1:
            charging.append((ta, tb))
    c.close()
    dd = derived()
    summary = activity_row(safe_one(dd, "SELECT * FROM activity_daily WHERE day=?", (day,))) if dd else None
    prof = safe_one(dd, "SELECT weight_kg FROM user_profile WHERE id=1") if dd else None
    if dd:
        dd.close()
    return {"date": day, "mode": ax.mode, "met": met_list, "hr": hr, "segments": segs,
            "sleep": sleep, "rests": rests, "charging": charging, "summary": summary,
            "weight_kg": prof["weight_kg"] if prof else None}


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
    "son activité récente, son journal (tags) et les signaux des dernières 24 h.\n"
    "Règles : réponds en français, concis et factuel ; n'utilise QUE les chiffres du contexte "
    "(si une donnée manque, dis-le) ; n'affirme aucune cause sans appui dans les données ou le "
    "journal ; aucun diagnostic médical — ces mesures sont des estimations non validées "
    "cliniquement ; en cas de symptômes inquiétants, oriente vers un médecin (urgence : 15 ou 112)."
)


def chat_context():
    ctx = {}
    d = derived()
    if d:
        keys = ("night", "score", "total", "deep", "rem", "efficiency", "latency", "waso", "awakenings",
                "timing", "hr_min", "hr_mean", "hrv_rmssd", "recovery_index", "resp_rate",
                "temp_mean", "temp_dev", "temp_status")
        ctx["nuits"] = [{k: r[k] for k in keys if r.get(k) is not None}
                        for r in reversed(safe_q(d, "SELECT * FROM sleep_scores ORDER BY night DESC LIMIT 30"))]
        rd = readiness_row(safe_one(d, "SELECT * FROM readiness ORDER BY day DESC LIMIT 1"))
        if rd:
            ctx["recuperation"] = rd
        ctx["activite"] = [{k: a[k] for k in ("day", "met_min_mh", "inactive_min", "active_kcal", "partial")}
                           for a in safe_q(d, "SELECT * FROM activity_daily ORDER BY day DESC LIMIT 7")]
        ctx["journal"] = safe_q(d, "SELECT day, kind, note FROM tags ORDER BY day DESC LIMIT 20")
        d.close()
    try:
        c = raw()
        ax = axis_for(c)
        ctx["anneau"] = {k: v for k, v in ring_status(c, ax).items() if k in ("battery", "state", "worn", "skin_temp")}
        c.close()
    except HTTPException:
        pass
    return json.dumps(ctx, ensure_ascii=False, default=str)


@app.post("/api/chat")
async def chat(body: dict):
    msg = (body.get("message") or "").strip()
    deep = bool(body.get("deep"))
    if not msg:
        raise HTTPException(400, "message vide")
    payload = {
        "model": LLM_MODEL_DEEP if deep else LLM_MODEL,
        "messages": [{"role": "system", "content": CHAT_SYSTEM + "\nContexte :\n" + chat_context()},
                     {"role": "user", "content": msg}],
        "temperature": 0.4,
        # qwen « thinking » : coupé pour le modèle rapide (sinon il consomme tout
        # le budget), conservé pour l'analyse approfondie avec un budget allongé
        "max_tokens": 2500 if deep else 800,
    }
    if not deep:
        payload["chat_template_kwargs"] = {"enable_thinking": False}
    try:
        async with httpx.AsyncClient(timeout=180) as cli:
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
