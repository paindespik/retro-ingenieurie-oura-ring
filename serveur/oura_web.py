#!/usr/bin/env python3
"""oura-web — portail complet des données de l'anneau Oura.

Lit (read-only) :
  /srv/oura/oura.db     — événements bruts décodés open_oura + scalaires (readings)
  /srv/oura/derived.db  — tables dérivées : sleep_scores, night_staging, llm_briefings, baselines
Chaque valeur est calculée localement. Le chat LLM passe par llama-swap (127.0.0.1:8012).

Tags open_oura utilisés ici (décimale) :
  65 ring_start · 66 time_sync · 69 state_change · 70 temp_event · 71 motion_event
  80 activity_information · 83 wear_event · 91 ble_connection · 93 hrv_event
  96 ibi_and_amplitude · 97 debug_data · 107 motion_period · 139 spo2_r_pi
"""
import base64
import json
import os
import sqlite3
import time
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse

RAW_DB = Path(os.environ.get("OURA_RAW_DB", "/srv/oura/oura.db"))
DERIVED_DB = Path(os.environ.get("OURA_DERIVED_DB", "/srv/oura/derived.db"))
INBOX_DIR = Path(os.environ.get("OURA_INBOX", "/srv/oura/inbox"))
LLM_URL = "http://127.0.0.1:8012/v1/chat/completions"
LLM_MODEL = "qwen3.5-4b"
LLM_MODEL_DEEP = "qwen3.8-27b-gsq"

# Nom d'événements (colonne `name` NOT NULL) — dérivé du tag à l'ingest.
TAG_NAMES = {65: "ring_start", 66: "time_sync", 69: "state_change", 70: "temp_event",
             71: "motion_event", 80: "activity_information", 83: "wear_event",
             91: "ble_connection", 93: "hrv_event", 96: "ibi_and_amplitude",
             97: "debug_data", 107: "motion_period", 128: "ibi_hr", 139: "spo2_r_pi"}

# Token par source au-dessus du Basic Auth nginx (header X-Oura-Token).
# JAMAIS de valeur en dur : le secret vient de l'environnement du service
# (EnvironmentFile=/etc/oura/ingest.env dans oura-web.service). Si la variable
# est absente, l'ingest refuse tout — plutôt qu'accepter un token connu.
INGEST_TOKENS = {
    s: t for s, t in {
        "phone": os.environ.get("OURA_PHONE_TOKEN", ""),
        "pc": os.environ.get("OURA_PC_TOKEN", ""),
    }.items() if t
}

app = FastAPI(title="oura-web")


@app.on_event("startup")
def ensure_push_telemetry():
    """Table de fraîcheur des pushes (une ligne par source)."""
    if RAW_DB.exists():
        c = sqlite3.connect(str(RAW_DB), timeout=15)
        try:
            c.execute("""CREATE TABLE IF NOT EXISTS push_telemetry (
                source         TEXT PRIMARY KEY,
                last_push_unix INTEGER,
                cursor         INTEGER,
                events_pushed  INTEGER,
                last_status    TEXT)""")
            c.commit()
        finally:
            c.close()


# ---------------------------------------------------------------- helpers

def q(c: sqlite3.Connection, sql: str, args=()):
    c.row_factory = sqlite3.Row
    return [dict(r) for r in c.execute(sql, args).fetchall()]


def one(c: sqlite3.Connection, sql: str, args=()):
    rows = q(c, sql, args)
    return rows[0] if rows else None


def jget(s):
    if not s:
        return None
    try:
        return json.loads(s)
    except Exception:
        return None


def raw():
    if not RAW_DB.exists():
        raise HTTPException(503, "oura.db absent — en attente du premier sync")
    c = sqlite3.connect(f"file:{RAW_DB}?mode=ro", uri=True)
    c.execute("PRAGMA busy_timeout=2000")
    return c


def derived():
    if not DERIVED_DB.exists():
        return None
    c = sqlite3.connect(f"file:{DERIVED_DB}?mode=ro", uri=True)
    c.execute("PRAGMA busy_timeout=2000")
    return c


PROFILE_TABLE = ("CREATE TABLE IF NOT EXISTS user_profile"
                " (id INTEGER PRIMARY KEY CHECK (id=1), age_years INTEGER,"
                " height_cm INTEGER, weight_kg REAL, sex TEXT, updated_unix INTEGER)")


def profile_rw():
    """derived.db en lecture/écriture (profil utilisateur, jamais effacé par le swap)."""
    c = sqlite3.connect(str(DERIVED_DB))
    c.execute("PRAGMA busy_timeout=2000")
    c.execute(PROFILE_TABLE)
    return c


def get_profile_row():
    if not DERIVED_DB.exists():
        return None
    c = profile_rw()
    r = one(c, "SELECT age_years, height_cm, weight_kg, sex, updated_unix FROM user_profile")
    c.close()
    return r


EPOCH_SLACK_DS = 6 * 3600 * 10   # port de open_oura tools/epoch_time.py
DRIFT_TOL_S = 900                # écart anneau↔serveur toléré sans correction (15 min)


def build_epochs(pairs):
    """Epochs de boot : ds croissant, coupure sur saut arrière > 6 h ; le ds max
    de chaque epoch est collé à l'heure de capture de l'événement qui le porte."""
    order = sorted((cu, ds) for ds, cu in pairs)
    epochs = []
    for cu, ds in order:
        if epochs and ds >= epochs[-1][1] - EPOCH_SLACK_DS:
            e = epochs[-1]
            if ds >= e[1]:
                e[1] = ds
                e[2] = cu
            e[0] = min(e[0], ds)
        else:
            epochs.append([ds, ds, cu])
    return epochs


def time_axis(c):
    """Mappe un timestamp anneau (uptime) sur l'heure unix réelle.

    Priorité : événements time_sync (tag 66, unix_time). À défaut : ancrage par
    epoch de boot (port de open_oura tools/epoch_time.py). À défaut : capture.
    Renvoie (t_of, mode) avec mode ∈ {"sync", "epoch", "capture"} ; t_of.drift_s
    porte l'écran anneau↔serveur appliqué (0 si < DRIFT_TOL_S).

    Le time_sync est écrit par l'hôte BLE (téléphone ou PC) avec sa propre
    horloge : si cette horloge dérive de celle du serveur (ex. téléphone sans
    NTP), tout l'axe est décalé et le filtre `since` (base serveur) renvoie
    l'historique complet quelle que soit la fenêtre. On cale donc l'axe sur
    l'heure de capture du dernier événement ingéré, qui relie les deux bases.
    """
    anchors = []
    for r in q(c, "SELECT ring_timestamp rt, decoded_json dj FROM events WHERE tag=66 ORDER BY ring_timestamp"):
        j = jget(r["dj"])
        if j and isinstance(j.get("unix_time"), (int, float)):
            anchors.append((r["rt"], int(j["unix_time"])))
    if anchors:
        # ring_timestamp est en DÉCISECONDES : le temps écoulé depuis une ancre
        # vaut (rt - art) / 10 s — l'additionner comme des secondes (rt + au - art)
        # étirait l'axe de ×10 entre deux syncs (cf. derive_night.time_axis, même
        # correction). L'ancre porte l'horloge de l'HÔTE BLE (téléphone/PC) : on
        # cale l'axe sur la captured_unix du dernier événement ingéré, qui relie
        # les deux bases, pour que le filtre `since` (base serveur) soit cohérent.
        last = one(c, "SELECT ring_timestamp rt, captured_unix cap FROM events"
                       " ORDER BY id DESC LIMIT 1")
        drift = 0
        if last:
            anc = None
            for art, au in anchors:
                if art <= last["rt"]:
                    anc = (art, au)
                else:
                    break
            if anc:
                drift = (anc[1] + (last["rt"] - anc[0]) / 10.0) - last["cap"]
        drift = drift if abs(drift) > DRIFT_TOL_S else 0

        def t_of(rt, cap):
            anchor = None
            for art, au in anchors:
                if art <= rt:
                    anchor = (art, au)
                else:
                    break
            if anchor is None:          # avant la 1re ancre : heure de capture
                return cap
            return anchor[1] + (rt - anchor[0]) / 10.0 - drift
        t_of.drift_s = drift
        return t_of, "sync"
    pairs = [(r["rt"], r["cap"]) for r in q(c, "SELECT ring_timestamp rt, captured_unix cap FROM events")]
    epochs = build_epochs(pairs)
    if epochs:
        def t_of(rt, cap):
            best = None
            for e in epochs:
                if e[0] - EPOCH_SLACK_DS <= rt <= e[1] + EPOCH_SLACK_DS:
                    span = e[1] - e[0]
                    if best is None or span < best[0]:
                        best = (span, e)
            e = best[1] if best else epochs[-1]
            return e[2] - (e[1] - rt) / 10.0
        t_of.drift_s = 0
        return t_of, "epoch"
    def t_of(rt, cap):
        return cap
    t_of.drift_s = 0
    return t_of, "capture"


def latest_event(c, tags, need_json=True):
    """Dernier événement (par ring_timestamp) d'un des tags, avec json décodé."""
    ph = ",".join("?" for _ in tags)
    where = f" WHERE tag IN ({ph})" + (" AND decoded_json IS NOT NULL" if need_json else "")
    r = one(c, f"SELECT tag, ring_timestamp rt, captured_unix cap, decoded_json dj FROM events"
               f"{where} ORDER BY ring_timestamp DESC, id DESC LIMIT 1", tuple(tags))
    if not r:
        return None
    r["decoded"] = jget(r["dj"])
    return r


def latest_battery(c):
    b = one(c, "SELECT value, unit, captured_unix t FROM readings WHERE kind='battery_percent'"
               " ORDER BY captured_unix DESC LIMIT 1")
    if b:
        return b
    # Les événements tag 97 récents sont souvent du bruit de debug : on filtre
    # directement sur la kind batterie plutôt que de scanner les 50 derniers.
    r = one(c, "SELECT decoded_json dj, captured_unix t FROM events"
            " WHERE tag=97 AND decoded_json LIKE '%battery_level_changed%'"
            " ORDER BY ring_timestamp DESC LIMIT 1")
    if r:
        j = jget(r["dj"])
        if j and j.get("kind") == "battery_level_changed":
            return {"value": j.get("battery_pct"), "unit": "%", "t": r["t"],
                    "voltage_mv": j.get("voltage_mv")}
    return None


# ---------------------------------------------------------------- API

@app.get("/api/overview")
def overview():
    c = raw()
    dev = q(c, "SELECT * FROM device")
    sync = q(c, "SELECT * FROM sync_state")
    cursor = sync[0]["next_cursor"] if sync else None
    boot = one(c, "SELECT ring_timestamp rt FROM events WHERE tag=65 ORDER BY ring_timestamp LIMIT 1")
    uptime = (cursor - boot["rt"]) if boot and cursor else None
    last_cap = one(c, "SELECT MAX(captured_unix) m FROM events")
    total_events = one(c, "SELECT COUNT(*) n FROM events")["n"]

    t_of, _ = time_axis(c)

    def pick(tag, fn):
        r = latest_event(c, [tag])
        if not r or r["decoded"] is None:
            return None
        v = fn(r["decoded"])
        if v is None:
            return None
        return {"t": t_of(r["rt"], r["cap"]), **v}

    temp = pick(70, lambda j: {"min": min(v), "max": max(v), "mid": v[len(v) // 2]}
                if (v := (j.get("temps_c") or [])) and len(v) >= 2 else None)
    met = pick(80, lambda j: {"met": max(v)} if (v := (j.get("met") or [])) else None)
    motion = pick(71, lambda j: {"seconds": j.get("motion_seconds"),
                                 "low": j.get("low_intensity"), "high": j.get("high_intensity")})
    state = latest_event(c, [69, 83])
    if state:
        j = state.pop("decoded") or {}
        state.update(state=j)
        state["t"] = t_of(state["rt"], state["cap"])
        state.pop("rt", None)
        state.pop("cap", None)

    hrv = pick(93, lambda j: {"hr": (j.get("hr_bpm") or [None])[-1],
                              "rmssd": (j.get("rmssd_ms") or [None])[-1]})
    ibi = latest_event(c, [128])
    hr_live = None
    if ibi and ibi["decoded"] and ibi["decoded"].get("hr_bpm"):
        hrs = ibi["decoded"]["hr_bpm"]
        hr_live = {"t": t_of(ibi["rt"], ibi["cap"]),
                   "hr": round(sum(hrs) / len(hrs), 1), "n": len(hrs)}
    spo2 = pick(139, lambda j: {"r": (j.get("r") or [None])[-1],
                                "pi": (j.get("perfusion_index") or [None])[-1]})

    battery = latest_battery(c)
    d = derived()
    last_night = one(d, "SELECT * FROM sleep_scores ORDER BY night DESC LIMIT 1") if d else None
    briefing = one(d, "SELECT * FROM llm_briefings ORDER BY ts DESC LIMIT 1") if d else None
    try:
        push_rows = q(c, "SELECT * FROM push_telemetry")
        push = {r["source"]: r for r in push_rows}
    except sqlite3.OperationalError:
        push = {}
    c.close()
    if d:
        d.close()
    return {
        "device": dev[0] if dev else None,
        "sync": sync[0] if sync else None,
        "uptime_s": uptime,
        "last_capture": last_cap["m"] if last_cap else None,
        "total_events": total_events,
        "push": push,
        "temp": temp, "met": met, "motion": motion, "state": state,
        "battery": battery,
        "hrv": hrv, "hr_live": hr_live, "spo2": spo2,
        "last_night": last_night, "briefing": briefing,
    }


@app.get("/api/telemetry")
def telemetry(hours: int = Query(6, ge=1, le=168)):
    c = raw()
    now = int(time.time())
    since = now - hours * 3600
    t_of, anchored = time_axis(c)

    def series(tags, pick):
        out = []
        for r in q(c, "SELECT tag, ring_timestamp rt, captured_unix cap, decoded_json dj FROM events"
                      " WHERE tag IN (%s) ORDER BY ring_timestamp" % ",".join("?" for _ in tags), tuple(tags)):
            t = t_of(r["rt"], r["cap"])
            if t < since:
                continue
            j = jget(r["dj"])
            if j is None:
                continue
            v = pick(r["tag"], j)
            if v is not None:
                out.append({"t": t, **v})
        return out

    def pick_temp(tag, j):
        v = j.get("temps_c") or []
        return {"min": min(v), "mid": v[len(v) // 2], "max": max(v)} if len(v) >= 2 else None

    def pick_met(tag, j):
        v = j.get("met") or []
        return {"met": max(v)} if v else None

    def pick_motion(tag, j):
        return {"seconds": j.get("motion_seconds"), "low": j.get("low_intensity"),
                "high": j.get("high_intensity")}

    def pick_state(tag, j):
        return {"kind": "wear" if tag == 83 else "state",
                "state": j.get("state"), "text": j.get("text")}

    # HR : hrv_event = N bins de 5 min (hr_bpm, rmssd_ms alignés)
    hr_pts = []
    for r in q(c, "SELECT ring_timestamp rt, captured_unix cap, decoded_json dj FROM events"
                 " WHERE tag=93 ORDER BY ring_timestamp"):
        t0 = t_of(r["rt"], r["cap"])
        if t0 + 300 * 8 < since:
            continue
        j = jget(r["dj"]) or {}
        hr, rm = j.get("hr_bpm") or [], j.get("rmssd_ms") or []
        for i in range(min(len(hr), len(rm))):
            t = t0 + i * 300
            if t >= since:
                hr_pts.append({"t": t, "hr": hr[i], "rmssd": rm[i]})
    # HR : green_ibi_quality (tag 128) = battements (ibi_ms), hr_bpm filtré qualité
    for r in q(c, "SELECT ring_timestamp rt, captured_unix cap, decoded_json dj FROM events"
                 " WHERE tag=128 ORDER BY ring_timestamp"):
        j = jget(r["dj"]) or {}
        ibi, hrs = j.get("ibi_ms") or [], j.get("hr_bpm") or []
        if not ibi or not hrs:
            continue
        t = t_of(r["rt"], r["cap"]) + sum(ibi) / 2000.0
        if t >= since:
            hr_pts.append({"t": t, "hr": round(sum(hrs) / len(hrs), 1), "rmssd": None})
    hr_pts.sort(key=lambda p: p["t"])

    # SpO2 : 1 Hz (R-ratio, perfusion index alignés)
    spo_pts = []
    for r in q(c, "SELECT ring_timestamp rt, captured_unix cap, decoded_json dj FROM events"
                 " WHERE tag=139 ORDER BY ring_timestamp"):
        t0 = t_of(r["rt"], r["cap"])
        if t0 + 60 * 20 < since:
            continue
        j = jget(r["dj"]) or {}
        rr, pi = j.get("r") or [], j.get("perfusion_index") or []
        for i in range(min(len(rr), len(pi))):
            t = t0 + i
            if t >= since:
                spo_pts.append({"t": t, "r": rr[i], "pi": pi[i]})
    spo_pts.sort(key=lambda p: p["t"])

    readings = [
        {"t": r["captured_unix"], "kind": r["kind"], "v": r["value"], "unit": r["unit"]}
        for r in q(c, "SELECT kind, value, unit, captured_unix FROM readings WHERE captured_unix >= ?"
                     " ORDER BY captured_unix", (since,))
    ]
    out = {
        "since": since, "now": now, "anchored": anchored,
        "drift_s": getattr(t_of, "drift_s", 0),
        "temp": series([70], pick_temp),
        "met": series([80], pick_met),
        "motion": series([71], pick_motion),
        "states": series([69, 83], pick_state),
        "hr": hr_pts,
        "spo2": spo_pts,
        "readings": readings,
    }
    c.close()
    return out


@app.get("/api/events")
def events(limit: int = Query(80, ge=1, le=500), type: str | None = None):
    c = raw()
    t_of, anchored = time_axis(c)
    sql = "SELECT tag, name, ring_timestamp rt, captured_unix cap, decoded_json dj FROM events"
    args = []
    if type:
        sql += " WHERE name=?"
        args.append(type)
    sql += " ORDER BY id DESC LIMIT ?"
    args.append(limit)
    rows = q(c, sql, tuple(args))
    types = q(c, "SELECT name, COUNT(*) n FROM events GROUP BY name ORDER BY n DESC")
    c.close()
    return {
        "anchored": anchored, "drift_s": getattr(t_of, "drift_s", 0),
        "events": [{"t": t_of(r["rt"], r["cap"]), "ring_ts": r["rt"], "tag": r["tag"],
                    "name": r["name"], "decoded": jget(r["dj"])} for r in rows],
        "types": types,
    }


@app.get("/api/activity")
def activity(date: str | None = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$")):
    """Résumé d'une journée depuis les bruts : bins MET 1 min, FC, segments d'effort.

    activity_information (tag 80) : 13 bins de 1 min par événement (intervalle
    observé 13 min). Segments = minutes consécutives à MET ≥ 3,0 (trou toléré
    ≤ 10 min), durée ≥ 5 min — équivalent local de la détection d'entraînement
    (l'appel activity-tagging cloud d'Oura est la seule étape cloud officielle).

    Retourne aussi les bruts du jour : mouvements (tag 71), port de l'anneau
    (tag 83), sessions de features (tag 108), scans capteurs (tag 130), SpO2
    horaire (tag 139).
    """
    from datetime import datetime
    c = raw()
    now = int(time.time())
    d0 = (datetime.strptime(date, "%Y-%m-%d") if date
          else datetime.fromtimestamp(now)).replace(hour=0, minute=0, second=0, microsecond=0)
    t0 = int(d0.timestamp())
    t1 = t0 + 86400
    t_of, mode = time_axis(c)

    met = {}
    for r in q(c, "SELECT ring_timestamp rt, captured_unix cap, decoded_json dj"
               " FROM events WHERE tag=80 AND decoded_json IS NOT NULL ORDER BY ring_timestamp"):
        j = jget(r["dj"])
        if not j:
            continue
        for i, v in enumerate(j.get("met") or []):
            t = int(round(t_of(r["rt"] + i * 600, r["cap"])))
            if t0 <= t < t1:
                met[t] = v
    met_list = [{"t": t, "met": v} for t, v in sorted(met.items())]

    hb = {}
    for r in q(c, "SELECT ring_timestamp rt, captured_unix cap, decoded_json dj"
               " FROM events WHERE tag IN (96,128) AND decoded_json IS NOT NULL ORDER BY ring_timestamp"):
        j = jget(r["dj"])
        if not j:
            continue
        beats = [b for b in (j.get("hr_bpm") or []) if 30 <= b <= 160]
        if not beats:
            continue
        ibi = j.get("ibi_ms") or []
        t = t_of(r["rt"] + (sum(ibi) / 2000.0 if ibi else 15.0), r["cap"])
        if t0 <= t < t1:
            hb.setdefault(int(t) - t0, []).append(sum(beats) / len(beats))
    hr = [{"t": t0 + m, "hr": round(sum(v) / len(v), 1)} for m, v in sorted(hb.items())]

    # Tranches d'effort : minutes à MET ≥ 3,0, trou toléré ≤ 10 min, span ≥ 5 min
    segs, cur = [], None
    for p in met_list:
        if p["met"] >= 3.0:
            if cur is None or p["t"] - cur["end"] > 600:
                if cur and (cur["end"] - cur["start"]) >= 300 and cur["n"] >= 4:
                    segs.append(cur)
                cur = {"start": p["t"], "end": p["t"], "s": p["met"], "n": 1}
            else:
                cur["end"] = p["t"]
                cur["s"] += p["met"]
                cur["n"] += 1
    if cur and (cur["end"] - cur["start"]) >= 300 and cur["n"] >= 4:
        segs.append(cur)
    for s in segs:
        s["min"] = round((s["end"] - s["start"]) / 60 + 1, 1)
        s["met_avg"] = round(s.pop("s") / s.pop("n"), 2)
        s["hr_mean"] = round(sum(p["hr"] for p in hr if s["start"] <= p["t"] <= s["end"]) /
                             max(1, sum(1 for p in hr if s["start"] <= p["t"] <= s["end"])), 1)

    # Mouvement (tag 71) : totaux du jour
    mot = {"seconds": 0, "high": 0, "low": 0, "events": 0}
    for r in q(c, "SELECT ring_timestamp rt, captured_unix cap, decoded_json dj"
               " FROM events WHERE tag=71 AND decoded_json IS NOT NULL"):
        t = t_of(r["rt"], r["cap"])
        if t0 <= t < t1:
            j = jget(r["dj"]) or {}
            mot["seconds"] += j.get("motion_seconds") or 0
            mot["high"] += j.get("high_intensity") or 0
            mot["low"] += j.get("low_intensity") or 0
            mot["events"] += 1

    # Port de l'anneau (tag 83) : mis / enlevé / charge
    wear = []
    for r in q(c, "SELECT ring_timestamp rt, captured_unix cap, decoded_json dj"
               " FROM events WHERE tag=83 AND decoded_json IS NOT NULL"):
        t = t_of(r["rt"], r["cap"])
        if t0 <= t < t1:
            j = jget(r["dj"]) or {}
            wear.append({"t": int(t), "state": j.get("state"), "text": j.get("text")})
    wear.sort(key=lambda w: w["t"])

    # Features de l'anneau (tag 108) : sessions de mesure
    feats = []
    for r in q(c, "SELECT ring_timestamp rt, captured_unix cap, decoded_json dj"
               " FROM events WHERE tag=108 AND decoded_json IS NOT NULL"):
        t = t_of(r["rt"], r["cap"])
        if t0 <= t < t1:
            j = jget(r["dj"]) or {}
            feats.append({"t": int(t), "feature_id": j.get("feature_id"),
                         "session_status": j.get("session_status"), "value": j.get("value")})
    feats.sort(key=lambda f: f["t"])

    # Scans capteurs (tag 130)
    scans_n = 0
    scans_last = None
    for r in q(c, "SELECT ring_timestamp rt, captured_unix cap FROM events WHERE tag=130"):
        t = t_of(r["rt"], r["cap"])
        if t0 <= t < t1:
            scans_n += 1
            scans_last = int(t) if scans_last is None else max(scans_last, int(t))

    # SpO2 (tag 139) : moyennes horaires R-ratio / perfusion
    spo = {}
    for r in q(c, "SELECT ring_timestamp rt, captured_unix cap, decoded_json dj"
               " FROM events WHERE tag=139 AND decoded_json IS NOT NULL"):
        ta = t_of(r["rt"], r["cap"])
        if ta + 60 < t0:
            continue
        j = jget(r["dj"]) or {}
        rr, pi = j.get("r") or [], j.get("perfusion_index") or []
        for i in range(min(len(rr), len(pi))):
            t = ta + i
            if t0 <= t < t1:
                d = spo.setdefault(int(t // 3600) * 3600, {"r": [], "pi": [], "n": 0})
                d["r"].append(rr[i]); d["pi"].append(pi[i]); d["n"] += 1
    spo2_hourly = [{"t": h, "r": round(sum(d["r"]) / len(d["r"]), 3),
                    "pi": round(sum(d["pi"]) / len(d["pi"]), 3), "n": d["n"]}
                   for h, d in sorted(spo.items())]

    mets = [p["met"] for p in met_list]
    stats = {
        "met_min_count": len(mets),
        "active_min": sum(1 for v in mets if v > 1.5),
        "met_minutes": round(sum(mets), 1) if mets else None,
        "met_max": max(mets) if mets else None,
        "hr_min": min(p["hr"] for p in hr) if hr else None,
        "hr_mean": round(sum(p["hr"] for p in hr) / len(hr), 1) if hr else None,
        "hr_max": max(p["hr"] for p in hr) if hr else None,
    }
    c.close()
    return {"date": d0.strftime("%Y-%m-%d"), "mode": mode, "met": met_list, "hr": hr,
            "stats": stats, "segments": segs, "motion": mot, "wear": wear,
            "features": feats, "scans": {"n": scans_n, "last": scans_last},
            "spo2_hourly": spo2_hourly}


@app.get("/api/nights")
def nights():
    d = derived()
    if not d:
        return {"nights": []}
    rows = q(d, "SELECT night, score, total, deep, rem, efficiency, latency, timing, restfulness"
               " FROM sleep_scores ORDER BY night")
    d.close()
    return {"nights": rows}


@app.get("/api/night")
def night(date: str = Query(..., pattern=r"^\d{4}-\d{2}-\d{2}$")):
    d = derived()
    if not d:
        raise HTTPException(503, "derived.db absent")
    score = one(d, "SELECT * FROM sleep_scores WHERE night=?", (date,))
    if not score:
        d.close()
        raise HTTPException(404, f"nuit {date} inconnue")
    prev = one(d, "SELECT score, total, deep, rem, efficiency FROM sleep_scores"
                 " WHERE night < ? ORDER BY night DESC LIMIT 1", (date,))
    staging = q(d, "SELECT epoch, stage FROM night_staging WHERE night=? ORDER BY epoch", (date,))
    brief = one(d, "SELECT * FROM llm_briefings WHERE night=? ORDER BY ts DESC LIMIT 1", (date,))
    d.close()
    return {"score": score, "prev": prev, "staging": staging, "briefing": brief}


@app.get("/api/trends")
def trends(days: int = Query(90, ge=7, le=365)):
    d = derived()
    if not d:
        raise HTTPException(503, "derived.db absent")
    rows = q(d, "SELECT night, score, total, deep, rem, efficiency, latency, timing,"
                " hr_min, hr_mean, hrv_rmssd, temp_dev, movement FROM sleep_scores"
                " ORDER BY night DESC LIMIT ?", (days,))
    base = {r["metric"]: r for r in q(d, "SELECT * FROM baselines")}
    d.close()
    return {"nights": list(reversed(rows)), "baselines": base}


@app.get("/api/briefing")
def briefing(date: str | None = None):
    d = derived()
    if not d:
        raise HTTPException(503, "derived.db absent")
    row = (one(d, "SELECT * FROM llm_briefings WHERE night=? ORDER BY ts DESC LIMIT 1", (date,))
           if date else one(d, "SELECT * FROM llm_briefings ORDER BY ts DESC LIMIT 1"))
    d.close()
    if not row:
        raise HTTPException(404, "aucun briefing")
    return row


def nights_context(days: int = 30):
    d = derived()
    if not d:
        return "Aucune nuit enregistrée pour l'instant."
    rows = q(d, "SELECT night, score, total, deep, rem, light, awake, efficiency, latency, timing,"
                " restfulness, hr_min, hr_mean, hrv_rmssd, temp_mean, temp_dev, movement,"
                " staging_source, subscores FROM sleep_scores"
                " ORDER BY night DESC LIMIT ?", (days,))
    d.close()
    return json.dumps(list(reversed(rows)), ensure_ascii=False)


def signals_context():
    """Résumé des 24 h de signaux bruts (contexte pour le chat LLM)."""
    try:
        c = raw()
    except HTTPException:
        return "{}"
    now = int(time.time())
    since = now - 86400
    out = {}
    temps, mets, motion_s = [], [], 0
    for tag, key, acc in ((70, "temps_c", temps), (80, "met", mets)):
        for r in q(c, "SELECT decoded_json dj FROM events WHERE tag=? AND captured_unix >= ?"
                     " AND decoded_json IS NOT NULL", (tag, since)):
            j = jget(r["dj"])
            if j:
                acc.extend(j.get(key) or [])
    for r in q(c, "SELECT decoded_json dj FROM events WHERE tag=71 AND captured_unix >= ?"
                 " AND decoded_json IS NOT NULL", (since,)):
        j = jget(r["dj"])
        if j:
            motion_s += j.get("motion_seconds") or 0
    if temps:
        out["temp_c_24h"] = {"min": round(min(temps), 2), "max": round(max(temps), 2)}
    if mets:
        out["met_max_24h"] = max(mets)
    out["motion_seconds_24h"] = motion_s
    st = latest_event(c, [69, 83])
    if st:
        j = st.get("decoded") or {}
        out["last_state"] = f"tag {st['tag']} état {j.get('state')} {j.get('text','')}".strip()
    bat = latest_battery(c)
    if bat:
        out["battery_pct"] = bat["value"]
    syn = one(c, "SELECT last_sync_unix FROM sync_state ORDER BY last_sync_unix DESC LIMIT 1")
    if syn:
        out["last_sync_min_ago"] = max(0, int((now - syn["last_sync_unix"]) // 60))
    hrv = latest_event(c, [93])
    if hrv and hrv.get("decoded"):
        j = hrv["decoded"]
        if j.get("hr_bpm"):
            out["hr_last_bpm"] = j["hr_bpm"][-1]
            out["rmssd_last_ms"] = (j.get("rmssd_ms") or [None])[-1]
    ibi = latest_event(c, [128])
    if ibi and ibi.get("decoded") and ibi["decoded"].get("hr_bpm"):
        hrs = ibi["decoded"]["hr_bpm"]
        out["hr_last_bpm"] = round(sum(hrs) / len(hrs), 1)
    c.close()
    return json.dumps(out, ensure_ascii=False)


@app.post("/api/chat")
async def chat(body: dict):
    msg = (body.get("message") or "").strip()
    deep = bool(body.get("deep"))
    if not msg:
        raise HTTPException(400, "message vide")
    system = (
        "Tu es l'analyste des données personnelles (sommeil + signaux vivants) de sean, "
        "collectées par son anneau Oura sans cloud. Tu reçois en contexte JSON : ses "
        "dernières nuits (score, sous-scores, FC, RMSSD, déviance thermique, latence, "
        "timing) et les signaux des 24 dernières heures (température de peau, MET, "
        "mouvement, batterie, dernier sync). Réponds en français, concis et factuel. "
        "INTERDICTION formelle d'inventer des chiffres absents du contexte : si une "
        "donnée manque, dis-le."
    )
    payload = {
        "model": LLM_MODEL_DEEP if deep else LLM_MODEL,
        "messages": [
            {"role": "system",
             "content": system + "\nNuits (30 dernières) :\n" + nights_context()
                                 + "\nSignaux 24 h :\n" + signals_context()},
            {"role": "user", "content": msg},
        ],
        "temperature": 0.4,
        # qwen3.x « thinking » : le raisonnement mangerait tout le budget →
        # on le coupe pour le modèle rapide ; le modèle profond garde ses
        # pensées mais avec un budget allongé.
        "max_tokens": 2500 if deep else 800,
    }
    if not deep:
        payload["chat_template_kwargs"] = {"enable_thinking": False}
    try:
        async with httpx.AsyncClient(timeout=180) as cli:
            r = await cli.post(LLM_URL, json=payload)
            r.raise_for_status()
            content = (r.json()["choices"][0]["message"].get("content") or "").strip()
            if not content:
                raise HTTPException(502, "llama-swap a renvoyé une réponse vide")
            return {"reply": content, "model": payload["model"]}
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"llama-swap indisponible : {e}") from e


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
        raise HTTPException(422, "sex invalide")
    if age is None and height is None and weight is None:
        raise HTTPException(422, "rien à enregistrer")
    c = profile_rw()
    c.execute("INSERT INTO user_profile VALUES (1, ?, ?, ?, ?, ?)"
              " ON CONFLICT(id) DO UPDATE SET age_years=excluded.age_years,"
              " height_cm=excluded.height_cm, weight_kg=excluded.weight_kg,"
              " sex=excluded.sex, updated_unix=excluded.updated_unix",
              (age, height, weight, sex, int(time.time())))
    c.commit()
    c.close()
    return {"ok": True}


@app.post("/ingest/events")
async def ingest_events(request: Request):
    """Push d'événements par une source (téléphone).

    Payload : {serial, cursor, pushed_unix, source?, events:[{tag, ring_timestamp,
    body(base64), decoded_json?, captured_unix?}]}
    Dédup sur UNIQUE(serial, tag, ring_timestamp, body) ; la télémétrie de
    fraîcheur (push_telemetry) alimente le portail et l'alerte > 2 h.
    """
    if not INGEST_TOKENS:
        raise HTTPException(
            503, "aucun token d'ingest configuré (OURA_PHONE_TOKEN absent — voir "
                 "/etc/oura/ingest.env)")
    token = request.headers.get("x-oura-token", "")
    src = next((s for s, t in INGEST_TOKENS.items() if t == token), None)
    if src is None:
        raise HTTPException(401, "X-Oura-Token invalide")
    p = await request.json()
    serial = str(p.get("serial") or "")
    events = p.get("events") or []
    cursor = int(p.get("cursor") or 0)
    now = int(time.time())
    c = sqlite3.connect(str(RAW_DB), timeout=15)
    c.execute("PRAGMA busy_timeout=15000")
    c.execute("""CREATE TABLE IF NOT EXISTS push_telemetry (
        source TEXT PRIMARY KEY, last_push_unix INTEGER, cursor INTEGER,
        events_pushed INTEGER, last_status TEXT)""")
    try:
        inserted = 0
        for e in events:
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
            (src, now, cursor, len(events), "ok"))
        c.commit()
        return {"inserted": inserted, "received": len(events), "source": src}
    finally:
        c.close()


@app.get("/api/health")
def health():
    c = raw()
    ui = one(c, "SELECT decoded_json dj FROM events WHERE name='user_information'"
             " ORDER BY ring_timestamp DESC LIMIT 1")
    out = {
        "devices": q(c, "SELECT * FROM device"),
        "sync": q(c, "SELECT * FROM sync_state"),
        "user_info": jget(ui["dj"]) if ui else None,
        "profile": get_profile_row(),
        "event_counts": q(c, "SELECT name, COUNT(*) n FROM events GROUP BY name ORDER BY n DESC"),
        "readings_counts": q(c, "SELECT kind, COUNT(*) n, MIN(captured_unix) first,"
                                " MAX(captured_unix) last FROM readings GROUP BY kind"),
        "files": {},
        "now": int(time.time()),
    }
    for p in (RAW_DB, DERIVED_DB, INBOX_DIR / "inbox-snapshot.db"):
        try:
            if p.exists():
                st = p.stat()
                out["files"][str(p)] = {"size": st.st_size, "mtime": int(st.st_mtime)}
        except OSError:
            pass
    d = derived()
    if d:
        tabs = {r["name"] for r in q(d, "SELECT name FROM sqlite_master WHERE type='table'")}
        out["derived_counts"] = {t: one(d, f"SELECT COUNT(*) n FROM {t}")["n"]
                                 for t in ("sleep_scores", "night_staging", "llm_briefings",
                                           "baselines") if t in tabs}
        d.close()
    c.close()
    return out


# ---------------------------------------------------------------- UI

HTML = """<!doctype html>
<html lang="fr"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>oura — sean</title>
<style>
:root{--bg:#fafafa;--fg:#222;--muted:#777;--card:#fff;--accent:#16a34a;--bad:#dc2626;--blue:#1d4ed8;--violet:#7c3aed}
@media (prefers-color-scheme:dark){:root{--bg:#111;--fg:#eee;--muted:#999;--card:#1c1c1c;--accent:#4ade80;--blue:#60a5fa;--violet:#a78bfa}}
*{box-sizing:border-box}body{margin:0;font:15px/1.5 system-ui;background:var(--bg);color:var(--fg)}
nav{display:flex;gap:.4rem;padding:.7rem 1rem;border-bottom:1px solid var(--muted);flex-wrap:wrap;align-items:baseline}
nav button{background:none;border:none;color:var(--muted);font-size:1rem;cursor:pointer;padding:.2rem .4rem}
nav button.on{color:var(--accent);border-bottom:2px solid var(--accent);font-weight:600}
nav .stamp{margin-left:auto;color:var(--muted);font-size:.8rem}
main{padding:1rem;max-width:1000px;margin:0 auto}
.card{background:var(--card);border-radius:10px;padding:1rem 1.2rem;margin:.8rem 0;box-shadow:0 1px 3px rgba(0,0,0,.08)}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:.8rem}
.grid .card{margin:0}
.big{font-size:2.6rem;font-weight:700}.sub{color:var(--muted);font-size:.85rem}
.kv{display:flex;justify-content:space-between;gap:1rem;padding:.18rem 0;border-bottom:1px dashed var(--muted)}
.kv:last-child{border-bottom:none}.kv b{font-variant-numeric:tabular-nums}
table{border-collapse:collapse;width:100%}td,th{padding:.25rem .5rem;text-align:left;border-bottom:1px solid var(--muted)}
th{color:var(--muted);font-weight:500}.num{text-align:right;font-variant-numeric:tabular-nums}
.rowline{display:flex;gap:.6rem;align-items:center;margin:.3rem 0}
.rowline .lbl{width:9.5rem;color:var(--muted);font-size:.85rem;flex:none}
.bar{flex:1;background:var(--muted);opacity:.25;border-radius:4px;height:.7rem;overflow:hidden}
.bar i{display:block;height:100%;background:var(--accent);border-radius:4px}
button.range{font:inherit;padding:.25rem .6rem;border-radius:8px;border:1px solid var(--muted);background:var(--card);color:var(--fg);cursor:pointer}
button.range.on{background:var(--accent);color:#fff;border-color:var(--accent)}
#chatlog div{margin:.4rem 0;padding:.6rem .9rem;border-radius:8px}
#chatlog .u{background:var(--accent);color:#fff;margin-left:20%}
#chatlog .a{background:var(--card);margin-right:20%}
input,select,button.act{font:inherit;padding:.5rem .8rem;border-radius:8px;border:1px solid var(--muted);background:var(--card);color:var(--fg)}
button.act{cursor:pointer;background:var(--accent);color:#fff;border:none}
svg text{fill:var(--muted);font-size:10px}
ul.tight li{margin:.2rem 0}
pre{font-size:.72rem;background:var(--bg);padding:.4rem;border-radius:6px;overflow-x:auto;white-space:pre-wrap;margin:.2rem 0}
details summary{cursor:pointer;color:var(--muted);font-size:.8rem}
table td details{display:inline-block;max-width:46rem}
</style></head><body>
<nav id="nav"></nav><main id="main"><div class="card">Chargement…</div></main>
<script>
const PAGES=["Vue d'ensemble","Activité","Nuits","Tendances","LLM","Événements","Santé"];
let page=0, evType=null, evLimit=100, range=6, curNights=[];
const $=s=>document.querySelector(s);
function nav(){const n=$("#nav");n.innerHTML="";PAGES.forEach((p,i)=>{const b=document.createElement("button");
 b.textContent=p;if(i===page)b.className="on";b.onclick=()=>{page=i;nav();render()};n.appendChild(b)});
 const s=document.createElement("span");s.className="stamp";s.id="stamp";n.appendChild(s)}
async function api(p,opt){const r=await fetch("/api/"+p,opt);if(!r.ok)throw new Error((await r.json()).detail||r.status);return r.json()}
function card(title,inner){return `<div class="card"><div class="sub">${title}</div>${inner||""}</div>`}
function fmt(v,d=1){return v==null?"—":Number(v).toFixed(d)}
function dt(t){return t==null?"—":new Date(t*1000).toLocaleString("fr-FR")}
function ago(t){if(t==null)return "—";const s=Date.now()/1000-t;if(s<60)return "à l'instant";
 if(s<3600)return `il y a ${Math.floor(s/60)} min`;if(s<86400)return `il y a ${Math.floor(s/3600)} h ${Math.floor(s%3600/60)} min`;
 return `il y a ${Math.floor(s/86400)} j`}
function fmtH(s){if(s==null)return "—";const h=Math.floor(s/3600);return h?h+" h "+Math.round(s%3600/60)+" min":Math.round(s/60)+" min"}
function kv(k,v){return `<div class="kv"><span>${k}</span><b>${v}</b></div>`}
function humanSize(b){if(b==null)return "—";if(b<1048576)return (b/1024).toFixed(0)+" Ko";return (b/1048576).toFixed(1)+" Mo"}
function profileForm(u,i){
 u=u||{};i=i||{};
 const age=u.age_years??i.age_years,h=u.height_cm??i.height_cm,w=u.weight_kg??i.weight_kg;
 const s=u.sex||(i.sex&&i.sex!=="unspecified"?i.sex:"unspecified");
 const opt=v=>`<option value="${v}"${s===v?" selected":""}>${v==="male"?"homme":v==="female"?"femme":"—"}</option>`;
 return `<div style="display:flex;gap:.6rem;flex-wrap:wrap;align-items:center">
  <label class="sub">Âge <input id="p_age" type="number" min="10" max="120" value="${age??""}"></label>
  <label class="sub">Taille (cm) <input id="p_h" type="number" min="80" max="230" value="${h??""}"></label>
  <label class="sub">Poids (kg) <input id="p_w" type="number" step="0.5" min="25" max="300" value="${w??""}"></label>
  <label class="sub">Sexe <select id="p_s">${opt("male")}${opt("female")}${opt("unspecified")}</select></label>
  <button class="act" id="p_save">Enregistrer</button><span id="p_status" class="sub"></span></div>
  <div class="sub" style="margin-top:.4rem">${u.updated_unix?"profil enregistré · mis à jour "+ago(u.updated_unix):"aucun profil enregistré — les valeurs ci-dessus sont celles inférées par l'anneau"}</div>
  <div class="sub">selon l'anneau (user_information${i._status==="inferred"?", inféré":""}) : ${i.age_years??"—"} ans · ${i.height_cm??"—"} cm · ${i.weight_kg??"—"} kg · ${i.sex&&i.sex!=="unspecified"?i.sex:"—"}</div>`}

function fmtTick(t,win){const d=new Date(t*1000);
 const hm=String(d.getHours()).padStart(2,"0")+":"+String(d.getMinutes()).padStart(2,"0");
 if(win>=48*3600)return d.toLocaleDateString("fr-FR",{day:"2-digit",month:"2-digit"})+" "+hm;
 if(win>=20*3600)return d.toLocaleDateString("fr-FR",{day:"2-digit",month:"2-digit"})+" "+String(d.getHours()).padStart(2,"0")+"h";
 return hm}

function svgLines(series,opt={}){
 const W=opt.W||860,H=opt.H||180;
 const has=opt.x0!=null&&opt.x1!=null&&opt.x1>opt.x0;
 const pts=series.flatMap(s=>s.points).filter(p=>!has||(p[0]>=opt.x0-60&&p[0]<=opt.x1+60));
 if(!pts.length)return `<div class="sub">pas encore de données</div>`;
 const xs=pts.map(p=>p[0]),ys=pts.map(p=>p[1]);
 const xmin=has?opt.x0:Math.min(...xs),xmax=has?opt.x1:Math.max(...xs);
 let ymin=opt.ymin??Math.min(...ys),ymax=opt.ymax??Math.max(...ys);
 if(xmax-xmin<1)xmax=xmin+1; if(ymax-ymin<1e-6){ymax+=1;ymin=Math.max(0,ymin-1)}
 const x=v=>(v-xmin)/(xmax-xmin)*(W-10)+5, y=v=>H-25-(v-ymin)/(ymax-ymin)*(H-45);
 let g=`<svg viewBox="0 0 ${W} ${H}" style="width:100%">`;
 for(let i=0;i<=4;i++){const yy=10+i*(H-45)/4,vv=ymax-i*(ymax-ymin)/4;
  g+=`<line x1="5" x2="${W-5}" y1="${yy}" y2="${yy}" stroke="var(--muted)" stroke-opacity=".25"/><text x="${W-8}" y="${yy+3}" text-anchor="end">${vv.toFixed(opt.dec??1)}</text>`}
 // axe X : graduations seulement si les abscisses sont des secondes unix
 // (Tendances passe des ms : xmin ~1.7e12) ; segments coupés sur les trous de
 // données pour éviter les fausses diagonales
 const win=xmax-xmin;
 if(xmin<1e11){const n=6;
  for(let i=0;i<=n;i++){const t=xmin+i*win/n,xx=5+i*(W-10)/n;
   g+=`<line x1="${xx}" x2="${xx}" y1="10" y2="${H-25}" stroke="var(--muted)" stroke-opacity=".12"/>`
     +`<text x="${xx}" y="${H-14}" text-anchor="${i===0?"start":i===n?"end":"middle"}">${fmtTick(t,win)}</text>`}}
 // seuil de coupure : au-dessus de 4× l'écart médian entre points de la série
 // (et jamais sous 4× le pas d'échantillonnage) → pas de fausses diagonales,
 // mais les points régulièrement espacés restent reliés
 const med=s=>{const d=[];for(let i=1;i<s.points.length;i++)d.push(s.points[i][0]-s.points[i-1][0]);
  return d.length?(d.sort((a,b)=>a-b)[Math.floor(d.length/2)]):0};
 const gap=Math.max(win/40,...series.map(s=>4*med(s)));
 series.forEach(s=>{const st=Math.max(1,Math.ceil(s.points.length/1800));
  const pp=s.points.filter((p,i)=>i%st===0||i===s.points.length-1);
  if(pp.length<2)return;
  let seg=[];
  const flush=()=>{if(seg.length>1)g+=`<polyline fill=none stroke="${s.color}" stroke-width=2 points="${seg.map(p=>x(p[0])+","+y(p[1])).join(" ")}"/>`;seg=[]};
  pp.forEach((p,i)=>{if(i&&p[0]-pp[i-1][0]>gap)flush();seg.push(p)});
  flush()});
 g+=`<text x=5 y=${H-4}>${dt(xmin)}</text><text x=${W-5} text-anchor=end y=${H-4}>${dt(xmax)}</text></svg>`;
 return g}

function svgBars(items,opt={}){
 const W=opt.W||860,H=opt.H||150;
 if(!items.length)return `<div class="sub">pas encore de données</div>`;
 const xs=items.map(i=>i.x),xmax=opt.xmax??Math.max(...xs),xmin=opt.xmin??Math.min(...xs);
 const ymax=Math.max(...items.map(i=>i.y),1);
 const x=v=>(v-xmin)/(xmax-xmin||1)*(W-10)+5;
 const w=Math.max(2,(W-10)/Math.max(1,items.length*2));
 let g=`<svg viewBox="0 0 ${W} ${H}" style="width:100%">`;
 (opt.bands||[]).forEach(b=>{g+=`<rect x="${x(b[0])}" y="10" width="${Math.max(2,x(b[1])-x(b[0]))}" height="${H-35}" fill="var(--violet)" opacity="0.14"/>`});
 items.forEach(i=>{const h=(H-45)*(i.y/ymax);
  g+=`<rect x="${x(i.x)-w/2}" y="${H-25-h}" width="${w}" height="${h}" fill="${i.color||"var(--accent)"}" rx="1"/>`});
 if(opt.threshold!=null){const ty=H-25-(opt.threshold/ymax)*(H-45);
  g+=`<line x1="5" x2="${W-5}" y1="${ty}" y2="${ty}" stroke="var(--bad)" stroke-dasharray="4 3" stroke-opacity="0.7"/>`
   +`<text x=${W-8} y=${ty-3} text-anchor=end>${opt.thresholdLabel||""}</text>`}
 g+=`<text x=${W-8} y=12 text-anchor=end>max ${ymax.toFixed(opt.dec??1)}</text>`;
 g+=`<text x=5 y=${H-4}>${dt(xmin)}</text><text x=${W-5} text-anchor=end y=${H-4}>${dt(xmax)}</text></svg>`;
 return g}

function stackedBars(items,opt={}){
 const W=opt.W||860,H=opt.H||170;
 if(!items.length)return `<div class="sub">pas encore de données</div>`;
 const has=opt.x0!=null&&opt.x1!=null&&opt.x1>opt.x0;
 const xmax=has?opt.x1:Math.max(...items.map(i=>i.x)),xmin=has?opt.x0:Math.min(...items.map(i=>i.x));
 const ymax=Math.max(...items.map(i=>i.act+i.hi),1);
 const x=v=>(v-xmin)/(xmax-xmin||1)*(W-10)+5;
 const w=Math.max(2,(W-10)/Math.max(1,(xmax-xmin)/3600*2.5));
 const lbl=t=>fmtTick(t,xmax-xmin);
 let g=`<svg viewBox="0 0 ${W} ${H}" style="width:100%">`;
 for(let i=0;i<=4;i++){const yy=10+i*(H-45)/4,vv=ymax-i*(ymax/4);
  g+=`<line x1="5" x2="${W-5}" y1="${yy}" y2="${yy}" stroke="var(--muted)" stroke-opacity=".25"/><text x="${W-8}" y="${yy+3}" text-anchor="end">${Math.round(vv)}</text>`}
 {const win=xmax-xmin,n=6;
  for(let i=0;i<=n;i++){const t=xmin+i*win/n,xx=5+i*(W-10)/n;
   g+=`<line x1="${xx}" x2="${xx}" y1="10" y2="${H-25}" stroke="var(--muted)" stroke-opacity=".12"/>`
     +`<text x="${xx}" y="${H-14}" text-anchor="${i===0?"start":i===n?"end":"middle"}">${lbl(t)}</text>`}}
 items.forEach(i=>{const hA=(H-45)*(i.act/ymax),hH=(H-45)*(i.hi/ymax);
  g+=`<rect x="${x(i.x)-w/2}" y="${H-25-hA}" width="${w}" height="${hA}" fill="var(--accent)" rx="1"/>`;
  g+=`<rect x="${x(i.x)-w/2}" y="${H-25-hA-hH}" width="${w}" height="${hH}" fill="var(--bad)" rx="1"/>`});
 g+=`<text x=${W-8} y=12 text-anchor=end>max ${Math.round(ymax)} s</text>`;
 g+=`<text x=5 y=${H-4}>${lbl(xmin)}</text><text x=${W-5} text-anchor=end y=${H-4}>${lbl(xmax)}</text></svg>`;
 return g}

function evText(name,d){
 if(!d)return "(non décodé)";
 switch(name){
  case "temp_event":return d.temps_c?"T° peau : "+d.temps_c.map(v=>v.toFixed(2)).join(" / ")+" °C":"";
  case "activity_information":return "MET "+(d.met||[]).join(", ")+" (état "+(d.state??"?")+")";
  case "motion_event":return "mouvement "+(d.motion_seconds??"?")+" s"+(d.low_intensity!=null?" · basse "+d.low_intensity:"")+(d.high_intensity!=null?" · haute "+d.high_intensity:"")+(d.orientation!=null?" · orientation "+d.orientation:"");
  case "motion_period":return "période "+(d.period_type??"?")+" : niveaux "+(d.motion_levels||[]).join("");
  case "wear_event":
  case "state_change":{
   const t=d.text||"",known={"chg. detected":"⚡ changement (prob. branché sur la charge)","chg. stopped":"charge terminée","orientation":"changement d'orientation"};
   const lbl=name==="wear_event"?"wear":"state_change";
   if(known[t])return known[t]+" ("+lbl+")";
   if(name==="wear_event"&&d.state===3&&/^\\d+$/.test(t))return lbl+" : anneau porté (prob.) · code interne «"+t+"»";
   if(/^\\d+$/.test(t))return lbl+" état "+d.state+" · code interne «"+t+"»";
   return lbl+" état "+d.state+(t?" · «"+t+"»":"");}
  case "ble_connection":return "lien BLE (subtype "+(d.subtype??"?")+")";
  case "ring_start":return "démarrage : fw "+(d.firmware_version??"?")+", raison "+(d.reason??"?");
  case "debug_event":return d.ascii||"";
  case "debug_data":return d.kind==="battery_level_changed"?"batterie "+d.battery_pct+" % ("+d.voltage_mv+" mV)"
   :d.kind==="charging_time"?"temps de charge "+d.charging_time
   :(d.ascii??"debug subtype "+(d.subtype??"?"));
  case "green_ibi_quality_event":return "FC : "+(d.hr_bpm||[]).slice(-8).join(", ")+" bpm (n="+(d.hr_bpm||[]).length+")";
  case "feature_session":return "session feature "+d.feature_id+" : statut "+d.session_status+(d.value!=null?" · valeur "+d.value:"");
  case "scan_start":return "début de scan";
  case "scan_end":return "fin de scan";
  case "hrv_event":return "FC "+(d.hr_bpm||[]).join(", ")+" bpm · RMSSD "+(d.rmssd_ms||[]).join(", ")+" ms (bins 5 min)";
  case "spo2_r_pi":return "R "+(d.r||[]).slice(0,4).join(", ")+(d.r.length>4?"…":"")+" · PI "+(d.perfusion_index||[]).slice(0,4).join(", ");
  case "time_sync":return "horloge anneau → unix "+(d.unix_time??"?");
  case "bedtime_period":return "fenêtre de sommeil détectée par l'anneau";
  case "user_information":return "profil anthropométrique";
  default:return JSON.stringify(d).slice(0,140);
 }
}

const FEATURES={2:"FC diurne (daytime_hr)"};
function featName(id){return FEATURES[id]||"feature "+(id??"")}
function wearTxt(w){const t=w.text||"";
 if(t==="chg. detected")return "⚡ changement (prob. branché sur la charge)";
 if(t==="chg. stopped")return "charge terminée";
 if(t==="orientation")return "orientation changée";
 if(w.state===3&&/^\\d+$/.test(t))return "anneau porté (probable) · code interne «"+t+"»";
 return "état "+(w.state??"?")+(t?" · «"+t+"»":"");}
function subscoreBar(label,v){
 const pct=v==null?0:Math.max(0,Math.min(100,v));
 return `<div class="rowline"><span class="lbl">${label}</span><div class="bar"><i style="width:${pct}%"></i></div><b style="width:2.5rem;text-align:right">${fmt(v,0)}</b></div>`}
function fmtHM(h){if(h==null)return"—";h=h%24;return String(Math.floor(h)).padStart(2,"0")+":"+String(Math.round((h%1)*60)).padStart(2,"0")}
function axisLbl(m,drift){let s=m==="sync"?"horloge ancrée (time_sync)":m==="epoch"?"horloge estimée (epoch de boot — pas de time_sync sur ce firmware)":"axe = heure de capture";
 if(drift&&Math.abs(drift)>900){const j=Math.floor(Math.abs(drift)/86400),h=Math.round(Math.abs(drift)%86400/3600);
  s+=` · horloge de l'hôte BLE en ${drift>0?"avance":"retard"} de ${j?j+" j ":""}${h} h sur le serveur — axe ramené à l'heure du serveur`}
 return s}
function jget(s){try{return s?JSON.parse(s):null}catch(e){return null}}

function hypnogram(st){
 if(!st.length)return `<div class="sub">aucun staging pour cette nuit</div>`;
 const W=860,H=150,c={"deep":"var(--blue)","rem":"var(--violet)","light":"#93c5fd","awake":"var(--bad)"};
 const n=st.length,x=i=>i/Math.max(1,n-1)*W,y=s=>({"deep":25,"rem":65,"light":105,"awake":135}[s]??105);
 let g=`<svg viewBox="0 0 ${W} ${H}" style="width:100%">`;
 let px=0,ps=st[0].stage;
 st.forEach((e,i)=>{if(e.stage!==ps){g+=`<line x1=${px} x2=${x(i)} y1=${y(ps)} y2=${y(ps)} stroke=${c[ps]||"#999"} stroke-width=11 />`;px=x(i);ps=e.stage}
  if(i===n-1)g+=`<line x1=${px} x2=${W} y1=${y(ps)} y2=${y(ps)} stroke=${c[ps]||"#999"} stroke-width=11 />`});
 g+=`<text x=2 y=22>Profond</text><text x=2 y=62>REM</text><text x=2 y=102>Léger</text><text x=2 y=132>Éveil</text></svg>`;
 return g}

async function render(){
 const m=$("#main");
 try{
  if(page===0){
   const o=await api("overview");
   m.innerHTML="";
   const dev=o.device,sync=o.sync;
   const stateLbl=o.state?evText(o.state.tag===83?"wear_event":"state_change",{text:o.state.text,state:o.state.state}):"—";
   let ring=`<div class="sub">${dev?dev.serial+" · fw "+dev.firmware:"aucun appareil"}</div>`
    +kv("État",stateLbl)
    +kv("Batterie",o.battery?fmt(o.battery.value,0)+" "+(o.battery.unit||"")+(o.battery.voltage_mv?" · "+o.battery.voltage_mv+" mV":""):"—")
    +kv("Uptime anneau",fmtH(o.uptime_s))
    +kv("Dernier sync",sync?ago(sync.last_sync_unix)+" (curseur "+sync.next_cursor+")":"—")
    +kv("Dernière capture",o.last_capture?ago(o.last_capture):"—")
    +kv("Push téléphone",o.push&&o.push.phone&&o.push.phone.last_push_unix?((Date.now()/1000-o.push.phone.last_push_unix>7200?"⚠️ ":"")+ago(o.push.phone.last_push_unix)+" ("+(o.push.phone.cursor??0)+")"):"—");
   const tempR=o.temp?`${fmt(o.temp.min,2)} – ${fmt(o.temp.max,2)} °C <span class="sub">(moy. ${fmt(o.temp.mid,2)})</span> · ${ago(o.temp.t)}`:"—";
   const metR=o.met?o.met.met+" <span class=sub>· "+ago(o.met.t)+"</span>":"—";
   const motR=o.motion?o.motion.seconds+" s"+(o.motion.high!=null?" (haute "+o.motion.high+")":"")+" · "+ago(o.motion.t):"—";
   const hrvR=o.hrv?o.hrv.hr+" bpm / "+o.hrv.rmssd+" ms":(o.hr_live?o.hr_live.hr+" bpm ("+o.hr_live.n+" battements)":"—");
   const spoR=o.spo2?o.spo2.r+" / PI "+o.spo2.pi:"—";
   const n=o.last_night;
   const nightCard=n
    ?(()=>{const sub=jget(n.subscores),B=k=>sub?sub[k]:null;
      return `<span class="big" style="color:${n.score>=70?'var(--accent)':'var(--bad)'}">${fmt(n.score,0)}</span><span class="sub">/100 · ${fmt(n.total,1)} h · ${n.night}</span>
      <div style="margin-top:.4rem">${subscoreBar("Durée",B("total_sleep"))+subscoreBar("Profond",B("deep"))+subscoreBar("REM",B("rem"))
      +subscoreBar("Efficacité",B("efficiency"))}</div>
      ${n.staging_source==="heuristique"?'<div class="sub" style="margin-top:.3rem">estimation (staging heuristique, non calibré)</div>':""}`})()
    :`<div class="sub">En attente de la première nuit complète — l'anneau n'a pas encore passé une nuit de données.</div>`;
   const brief=o.briefing?`<div class="sub">${o.briefing.night} · ${o.briefing.model}</div><div style="margin-top:.3rem">${o.briefing.text}</div>`
    :`<div class="sub">Aucun briefing pour l'instant (généré chaque matin à 05:35 par le LLM local).</div>`;
   m.innerHTML+=`<div class="grid">
    <div class="card"><div class="sub">L'anneau</div>${ring}</div>
    <div class="card"><div class="sub">Signaux récents</div>
     ${kv("T° de peau",tempR)}${kv("Activité (MET)",metR)}${kv("Mouvement",motR)}
     ${kv("FC / HRV",hrvR)}${kv("SpO2 (R / PI)",spoR)}</div></div>`
    +card("Dernière nuit",nightCard)+card("Dernier briefing LLM",brief);
   const e=await api("events?limit=6").catch(()=>null);
   if(e&&e.events.length)m.innerHTML+=card("Derniers événements ("+e.events.length+" de "+o.total_events+")",
    e.events.map(ev=>`<div class="kv"><span class="sub">${dt(ev.t)} · ${ev.name}</span><span>${evText(ev.name,ev.decoded)}</span></div>`).join(""));
   $("#stamp").textContent="dernières données : "+ago(o.last_capture);
  }

  if(page===1){
   let html="";
   try{
    const a=await api("activity"+(window.adate?"?date="+window.adate:""));
    window.adate=a.date;
    html+=card("Résumé du jour — "+a.date+` <input type="date" id="ad" value="${a.date}" style="padding:.25rem"> <button class=act onclick="window.adate=document.getElementById('ad').value;render()">Afficher</button>`,
     `<div class="rowline"><span class="lbl">Minutes actives (MET &gt; 1,5)</span><b>${a.stats.active_min} min</b></div>
      <div class="rowline"><span class="lbl">MET-minutes (Σ) · MET max</span><b>${a.stats.met_minutes??"—"} · ${a.stats.met_max==null?"—":a.stats.met_max}</b></div>
      <div class="rowline"><span class="lbl">FC min / moy / max</span><b>${a.stats.hr_min??"—"} / ${a.stats.hr_mean??"—"} / ${a.stats.hr_max??"—"} bpm</b></div>
      <div class="rowline"><span class="lbl">Mouvement</span><b>${fmtH(a.motion.seconds)} actives (dont ${fmtH(a.motion.high)} haute intensité) · ${a.motion.events} fenêtres</b></div>
      <div class="rowline"><span class="lbl">Scans capteurs</span><b>${a.scans.n} (dernier ${a.scans.last?dt(a.scans.last):"—"})</b></div>`
     +(a.segments.length?`<div class="sub" style="margin-top:.5rem">Tranches d'effort (MET ≥ 3, trous ≤ 10 min)</div>
      <table>${a.segments.map(s=>`<tr><td>${dt(s.start)} → ${dt(s.end)}</td><td class="num">${s.min} min</td><td class="num">MET ${s.met_avg}</td><td class="num">${s.hr_mean??"—"} bpm</td></tr>`).join("")}</table>`:"")
     +`<div class="sub" style="margin-top:.4rem">${a.met.length?"":"aucune donnée MET sur ce jour · "}${axisLbl(a.mode)}</div>`);
    if(a.met.length)html+=card("Intensité (MET, bins 1 min)",
     svgBars(a.met.map(p=>({x:p.t,y:p.met,color:p.met>=6?"var(--bad)":p.met>=3?"var(--violet)":"var(--accent)"})),
      {dec:1,ymin:0,threshold:3,thresholdLabel:"seuil 3,0",bands:a.segments.map(s=>[s.start,s.end])})
     +`<div class="sub">vert < 3 (repos) · violet 3–6 (actif) · rouge > 6 (intense) · ombrage violet + pointillés = tranches d'effort détectées</div>`);
    if(a.wear.length)html+=card("Port de l'anneau",
     a.wear.map(w=>`<div class="kv"><span class="sub">${dt(w.t)}</span><span>${wearTxt(w)}</span></div>`).join(""));
    if(a.features.length)html+=card(`Features de l'anneau (sessions de mesure)`,
     `<details><summary class=sub>${a.features.length} sessions — afficher</summary><table>`
     +a.features.map(f=>`<tr><td class="sub">${dt(f.t)}</td><td>${featName(f.feature_id)}</td><td class="num">statut ${f.session_status}</td><td class="num">${f.value??"—"}</td></tr>`).join("")
     +"</table><div class='sub'>feature_session bruts : statut de démarrage/fin de chaque mesure de l'anneau (FC diurne, SpO2…)</div></details>");
    if(a.spo2_hourly.length)html+=card("SpO2 (moyennes par heure)",
     `<table><tr><th>Heure</th><th class=num>R-ratio</th><th class=num>Perfusion</th><th class=num>Échantillons</th></tr>`
     +a.spo2_hourly.map(s=>`<tr><td class="sub">${dt(s.t)}</td><td class="num">${fmt(s.r,3)}</td><td class="num">${fmt(s.pi,3)}</td><td class="num">${s.n}</td></tr>`).join("")
     +"</table><div class='sub'>mesures 1 Hz pendant les scans SpO2 (R-ratio et perfusion index bruts, sans calcul de saturation)</div>");
   }catch(e){html+=card("Résumé du jour","<div class='sub'>"+String(e.message)+"</div>")}
   const H=[1,3,6,24,168],LBL={1:"1 h",3:"3 h",6:"6 h",24:"24 h",168:"7 j"};
   const t=await api("telemetry?hours="+range);
   html+=`<div class="card" style="display:flex;gap:.4rem;align-items:center">`
    +H.map(h=>`<button class="range ${h===range?"on":""}" onclick="setRange(${h})">${LBL[h]}</button>`).join("")
    +`<span class="sub" style="margin-left:auto">${axisLbl(t.anchored,t.drift_s)}</span></div>`;
   if(t.temp.length){
    html+=card("Température de peau (min / médiane / max)",
     svgLines([{color:"var(--bad)",points:t.temp.map(p=>[p.t,p.min])},
               {color:"var(--accent)",points:t.temp.map(p=>[p.t,p.mid])},
               {color:"var(--blue)",points:t.temp.map(p=>[p.t,p.max])}],{dec:2,x0:t.since,x1:t.now}));
   } else html+=card("Température de peau","<div class='sub'>pas encore de temp_event sur la période</div>");
   if(t.met.length)html+=card("Activité (MET)",svgLines([{color:"var(--accent)",points:t.met.map(p=>[p.t,p.met])}],{dec:2,ymin:0,x0:t.since,x1:t.now}));
   if(t.motion.length){
    const hh={};
    t.motion.forEach(p=>{const h=Math.floor(p.t/3600)*3600;const d=hh[h]??={s:0,hi:0};
     d.s+=p.seconds||0;d.hi+=p.high||0});
    html+=card("Mouvement par heure (s actives, rouge = s haute intensité)",
     stackedBars(Object.entries(hh).map(([h,d])=>({x:+h,act:d.s,hi:d.hi})),{x0:t.since,x1:t.now})
     +`<div class="sub">chaque barre = 1 heure · vert = secondes actives (fenêtres de 30 s) · rouge = secondes haute intensité (balancier du bras : vélo, marche rapide…)</div>`);
   }
   if(t.states.length)html+=card(`États (wear / charge / orientation) — ${t.states.length} événements`,
    `<div style="max-height:14rem;overflow-y:auto;border:1px solid var(--muted);border-radius:8px;padding:.2rem .6rem">`
    +t.states.map(s=>`<div class="kv"><span class="sub">${dt(s.t)}</span><span>${evText(s.kind==="wear"?"wear_event":"state_change",{text:s.text,state:s.state})}</span></div>`).join("")
    +`</div>`);
   if(t.hr.length)html+=card("Fréquence cardiaque / HRV",
    svgLines([{color:"var(--bad)",points:t.hr.map(p=>[p.t,p.hr])},
              {color:"var(--violet)",points:t.hr.filter(p=>p.rmssd!=null).map(p=>[p.t,p.rmssd])}],{dec:0,x0:t.since,x1:t.now})
    +`<div class="sub">rouge = FC (bpm) · violet = RMSSD (ms, bins 5 min)</div>`);
   else html+=card("Fréquence cardiaque / HRV","<div class='sub'>pas encore d'événements hrv — les features daytime_hr / SpO2 sont actives, les données arriveront aux syncs suivants</div>");
   if(t.spo2.length)html+=card("SpO2 — R-ratio (mesures 1 Hz pendant les scans SpO2)",
    svgLines([{color:"var(--blue)",points:t.spo2.map(p=>[p.t,p.r])}],{x0:t.since,x1:t.now})
    +`<div class='sub'>R = (AC/DC)<sub>rouge</sub> / (AC/DC)<sub>IR</sub> · scans burst par périodes, pas en continu — SpO₂ ≈ 110 − 25·R (méd. tes mesures ≈ 93 %)</div>`);
   if(t.readings.length)html+=card("Mesures scalaires (readings)",
    "<table>"+t.readings.map(r=>`<tr><td class="sub">${dt(r.t)}</td><td>${r.kind}</td><td class="num">${fmt(r.v)} ${r.unit||""}</td></tr>`).join("")+"</table>");
   else html+=card("Mesures scalaires (readings)","<div class='sub'>aucune mesure scalaire stockée pour l'instant (batterie, FC live, SpO2…)</div>");
   m.innerHTML=html;
  }

  if(page===2){
   const n=await api("nights");
   if(!n.nights.length){
    m.innerHTML=card("Nuits — intégralité",
     `<p>En attente de la première nuit complète de l'anneau (démarré récemment). À partir de la 1re nuit, cette page affichera :</p>
      <ul class="tight">
      <li><b>Score global /100</b> + les 6 sous-scores : profond, REM, efficacité, latence, timing, restfulness</li>
      <li>heure de coucher → lever, <b>durée totale</b>, efficacité, latence d'endormissement, timing (midpoint)</li>
      <li>FC min / moyenne, <b>RMSSD</b> (variabilité), déviance <b>température</b> vs baseline, mouvement (MAD), SpO2</li>
      <li><b>hypnogramme</b> stades (profond / léger / REM / éveil)</li>
      <li>source du staging (mesuré par l'anneau ou estimé) + <b>analyse LLM de la nuit</b></li></ul>
      <p class="sub">En attendant : la télémétrie en direct (T° peau, MET, mouvement, FC…) est sur l'onglet <b>Activité</b>.</p>`);
    return;
   }
   if(!curNights.length)curNights=n.nights;
   m.innerHTML=`<div class="card"><div class="sub">Choisir une nuit</div>
    <select id="nd" style="min-width:10rem"></select><div id="nbody" style="margin-top:.8rem"></div></div>`;
   const sel=$("#nd");
   curNights.slice().reverse().forEach(x=>{const o=document.createElement("option");
    o.value=x.night;o.textContent=x.night+" — score "+fmt(x.score,0);sel.appendChild(o)});
   sel.onchange=async()=>{
    const d=await api("night?date="+sel.value);
    const s=d.score,p=d.prev||{};
    const delta=p.score!=null?`<span class="sub">(${(s.score-p.score)>=0?"+":""}${fmt(s.score-p.score,1)} vs nuit précédente)</span>`:"";
    $("#nbody").innerHTML=
     `<span class="big" style="color:${s.score>=70?'var(--accent)':'var(--bad)'}">${fmt(s.score,0)}</span><span class="sub">/100 ${delta}</span>
      ${(()=>{const sub=jget(s.subscores),B=k=>sub?sub[k]:null;
      return subscoreBar("Durée",B("total_sleep"))+subscoreBar("Profond",B("deep"))+subscoreBar("REM",B("rem"))
      +subscoreBar("Efficacité",B("efficiency"))+subscoreBar("Latence",B("latency"))
      +subscoreBar("Timing",B("timing"))+subscoreBar("Restfulness",B("restfulness"))})()}
      <table style="margin-top:.6rem">
      <tr><th>Coucher → lever</th><td class="num">${s.bedtime||"—"} → ${s.wake_time||"—"}</td><th>Durée / en lit</th><td class="num">${fmt(s.total,1)} h / ${fmt(s.in_bed,1)} h</td></tr>
      <tr><th>Léger / éveil</th><td class="num">${fmt(s.light,1)} h / ${fmt(s.awake,1)} h</td><th>Efficacité</th><td class="num">${fmt(s.efficiency,0)} %</td></tr>
      <tr><th>Latence</th><td class="num">${fmt(s.latency,0)} min</td><th>Timing (midpoint)</th><td class="num">${fmtHM(s.timing)}</td></tr>
      <tr><th>FC min / moy</th><td class="num">${fmt(s.hr_min,0)} / ${fmt(s.hr_mean,0)} bpm</td><th>RMSSD</th><td class="num">${fmt(s.hrv_rmssd,0)} ms</td></tr>
      <tr><th>Dév. température</th><td class="num">${fmt(s.temp_dev,2)} °C</td><th>Mouvement</th><td class="num">${fmt(s.movement,1)} min</td></tr>
      <tr><th>SpO2</th><td class="num">${s.spo2==null?"—":fmt(s.spo2,0)+" %"}</td><th>Staging</th><td class="num">${s.staging_source||"—"}</td></tr></table>
      ${s.staging_source==="heuristique"?`<div class="sub" style="margin-top:.5rem">Stages estimés (mouvement + FC) · score : anchors publiés + poids officiels Oura — approximation non calibrée (calibration fine prévue après 2–3 nuits).</div>`:""}
      <div style="margin-top:.8rem">${hypnogram(d.staging)}</div>
      ${d.briefing?card("Analyse LLM de la nuit ("+d.briefing.model+")","<div>"+d.briefing.text+"</div>"):""}`;
   };
   sel.onchange();
  }

  if(page===3){
    if(!window.tdays)window.tdays=90;
    const D=[30,90];
    const t=await api("trends?days="+window.tdays);
    curNights=t.nights;
    let html=`<div class="card" style="display:flex;gap:.4rem;align-items:center">
    ${D.map(d=>`<button class="range ${d===window.tdays?"on":""}" onclick="setTrends(${d})">${d} nuits</button>`).join("")}
    <span class="sub" style="margin-left:auto">${t.nights.length} nuits disponibles</span></div>`;
    if(t.nights.length){
     html+=card("Score de sommeil",svgLines([{color:"var(--accent)",points:t.nights.map(n=>[Date.parse(n.night)/1000,n.score])}],{ymin:0,ymax:100,dec:0}));
     html+=card("Toutes les nuits",`<table><tr><th>Nuit</th><th class=num>Score</th><th class=num>Durée</th><th class=num>Profond</th><th class=num>REM</th>
     <th class=num>Effic.</th><th class=num>FC moy</th><th class=num>RMSSD</th><th class=num>Temp δ</th></tr>`
     +t.nights.map(n=>`<tr><td>${n.night}</td><td class=num>${fmt(n.score,0)}</td><td class=num>${fmt(n.total,1)}</td><td class=num>${fmt(n.deep,1)}</td><td class=num>${fmt(n.rem,1)}</td><td class=num>${fmt(n.efficiency,0)}</td><td class=num>${fmt(n.hr_mean,0)}</td><td class=num>${fmt(n.hrv_rmssd,0)}</td><td class=num>${fmt(n.temp_dev,2)}</td></tr>`).join("")+"</table>");
    } else html+=card("Tendances","<div class='sub'>pas encore de nuits — les tendances (30/90 j) apparaîtront après les premières nuits.</div>");
    m.innerHTML=html;
  }

  if(page===4){
   m.innerHTML=card("Interroger mes données","<div id='chatlog'></div><div style='display:flex;gap:.5rem;flex-wrap:wrap'><input id='chatin' style='flex:1;min-width:12rem' placeholder='ex: comment a été ma dernière nuit ? / pourquoi ma FC de nuit monte ?'><button class=act onclick=send(false)>Envoyer</button><button class=act onclick=send(true) style='background:#555'>Analyse profonde</button></div>");
   const b=await api("briefing").catch(()=>null);
   if(b)$("#chatlog").innerHTML=`<div class=a><b>Briefing du ${b.night} (${b.model})</b><br>${b.text}</div>`;
   $("#chatin").addEventListener("keydown",e=>{if(e.key==="Enter")send(false)});
  }

  if(page===5){
   const e=await api("events?limit="+evLimit+(evType?"&type="+encodeURIComponent(evType):""));
   m.innerHTML=`<div class="card" style="display:flex;gap:.6rem;align-items:center;flex-wrap:wrap">
    <select id="evf" style="min-width:12rem"></select>
    <span class="sub">filtre par type d'événement</span>
    <select id="evl">${[100,300,500].map(n=>`<option value="${n}" ${n===evLimit?"selected":""}>${n} évts</option>`).join("")}</select>
    <span class="sub">affichage</span>
    <button class=act style="margin-left:auto" onclick=render()>Rafraîchir</button></div>`;
   const sel=$("#evf");
   const all=document.createElement("option");all.value="";all.textContent="tous les types";sel.appendChild(all);
   e.types.forEach(t=>{const o=document.createElement("option");o.value=t.name;
    o.textContent=t.name+" ("+t.n+")";sel.appendChild(o)});
   sel.value=evType||"";
   sel.onchange=()=>{evType=sel.value;render()};
   $("#evl").onchange=e2=>{evLimit=+e2.target.value;render()};
   m.innerHTML+=card("Événements bruts décodés (derniers "+e.events.length+")",
    `<div class="sub" style="margin-bottom:.4rem">${axisLbl(e.anchored,e.drift_s)}</div>
     <table><tr><th>Heure</th><th>Type</th><th>Contenu</th></tr>`
    +e.events.map(ev=>`<tr><td class="sub" style="white-space:nowrap">${dt(ev.t)}</td><td>${ev.name}</td><td>${evText(ev.name,ev.decoded)}${ev.decoded?`<details><summary>JSON brut</summary><pre>${JSON.stringify(ev.decoded,null,1)}</pre></details>`:""}</td></tr>`).join("")+"</table>");
  }

  if(page===6){
   const h=await api("health");
   m.innerHTML=
    card("Appareil",(h.devices[0]?`${h.devices[0].serial} · fw ${h.devices[0].firmware} · api ${h.devices[0].api_version} · ${h.devices[0].mac}`:"aucun"))
   +card("Sync",h.sync[0]?`curseur <b>${h.sync[0].next_cursor}</b> · dernier sync <b>${ago(h.sync[0].last_sync_unix)}</b>`:"jamais synchronisé")
   +card("Événements en base","<table>"+h.event_counts.map(e=>`<tr><td>${e.name}</td><td class=num>${e.n}</td></tr>`).join("")+"</table>")
   +card("Mesures scalaires (readings)",h.readings_counts.length
     ?"<table>"+h.readings_counts.map(r=>`<tr><td>${r.kind}</td><td class=num>${r.n}</td><td class=sub">${dt(r.last)}</td></tr>`).join("")+"</table>"
     :"<div class='sub'>aucune (batterie, FC live, SpO2… arriveront)</div>")
   +card("Tables dérivées",h.derived_counts?Object.entries(h.derived_counts).map(([k,v])=>kv(k,v)).join(""):"<div class='sub'>derived.db absent</div>")
   +card("Profil",profileForm(h.profile,h.user_info))
   +card("Fichiers",Object.entries(h.files).map(([p,f])=>kv(p.split("/").pop(),humanSize(f.size)+" · modifié "+ago(f.mtime))).join(""));
   $("#p_save").onclick=async()=>{
    const st=$("#p_status");st.textContent="…";
    try{
     await api("profile",{method:"POST",headers:{"Content-Type":"application/json"},
      body:JSON.stringify({age_years:$("#p_age").value||null,height_cm:$("#p_h").value||null,
                          weight_kg:$("#p_w").value||null,sex:$("#p_s").value})});
     st.textContent="✓ enregistré";render();
    }catch(e){st.textContent="⚠ "+e.message}
   };
  }
 }catch(e){m.innerHTML=card("Erreur",String(e.message))}
}

function setRange(h){range=h;render()}
function setTrends(d){window.tdays=d;render()}
async function send(deep){const inp=$("#chatin"),t=inp.value.trim();if(!t)return;inp.value="";
 const log=$("#chatlog");log.insertAdjacentHTML("beforeend","<div class=u></div>");log.lastChild.textContent=t;
 log.insertAdjacentHTML("beforeend","<div class=a>…</div>");
 try{const r=await api("chat",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({message:t,deep})});
  log.lastChild.textContent=r.reply+"\\n\\n["+r.model+"]"}catch(e){log.lastChild.textContent="⚠ "+e.message}}
// deep-link : #page=N&range=H préselectionne l'onglet et la plage (Activité)
{const m=location.hash.match(/page=(\\d+)/);if(m)page=Math.min(+m[1],PAGES.length-1);
 const r=location.hash.match(/range=(\\d+)/);if([1,3,6,24,168].includes(+r?.[1]))range=+r[1]}
nav();render();
</script></body></html>"""


@app.get("/", response_class=HTMLResponse)
def index():
    return HTMLResponse(HTML)
