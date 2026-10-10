"""oura_core — briques partagées par le portail (oura_web) et le job nightly (derive_night).

Bibliothèque standard uniquement : le portail tourne dans un venv minimal
(fastapi/uvicorn/httpx) et le job nightly avec le python3 du système.

Conventions des données brutes (open_oura, décodées à l'ingestion) :
- `ring_timestamp` (rt) est en DÉCISECONDES depuis le démarrage de l'anneau ;
- les événements agrégés portent l'horodatage de FIN de leur fenêtre :
  `hrv_event` (N bins de 5 min) et `activity_information` (N bins de 1 min)
  couvrent les N×pas qui PRÉCÈDENT rt (vérifié par recoupement avec les
  battements du tag 96 et avec le mouvement du tag 71).
"""
import bisect
import json
import math
import statistics

# ---------------------------------------------------------------- tags (décimal)
T_RING_START = 65
T_TIME_SYNC = 66
T_DEBUG_EVENT = 67
T_STATE = 69
T_TEMP = 70
T_MOTION = 71
T_ACTIVITY = 80
T_WEAR = 83
T_BLE = 91
T_USER_INFO = 92
T_HRV = 93
T_IBI = 96
T_DEBUG_DATA = 97
T_TEMP_PERIOD = 105
T_MOTION_PERIOD = 107
T_FEATURE = 108
T_ACM = 114
T_SLEEP_TEMP = 117
T_BEDTIME = 118
T_SELF_TEST = 121
T_GREEN_IBI = 128
T_SCAN_START = 130
T_SCAN_END = 131
T_RTC = 133
T_SPO2_RPI = 139

TAG_NAMES = {65: "ring_start", 66: "time_sync", 67: "debug_event", 69: "state_change",
             70: "temp_event", 71: "motion_event", 80: "activity_information",
             83: "wear_event", 91: "ble_connection", 92: "user_information",
             93: "hrv_event", 96: "ibi_and_amplitude_event", 97: "debug_data",
             105: "temp_period", 107: "motion_period", 108: "feature_session",
             114: "sleep_acm_period", 117: "sleep_temp_event", 118: "bedtime_period",
             121: "self_test_data_event", 128: "green_ibi_quality_event",
             130: "scan_start", 131: "scan_end", 133: "rtc_beacon", 139: "spo2_r_pi_event"}

# Types de bas niveau (diagnostic firmware) masqués par défaut dans le portail.
NOISY_EVENT_NAMES = ("debug_event", "debug_data", "ble_connection", "self_test_data_event",
                     "scan_start", "scan_end", "feature_session", "motion_period")

HRV_BIN_S = 300          # hrv_event : 1 bin = 5 min
MET_BIN_S = 60           # activity_information : 1 bin = 1 min

# Bornes physiologiques : en dehors, la valeur est un artefact et n'est ni
# affichée ni utilisée dans un calcul.
HR_MIN_BPM, HR_MAX_BPM = 30, 200
RMSSD_MIN_MS, RMSSD_MAX_MS = 3, 250
SKIN_TEMP_MIN_C, SKIN_TEMP_MAX_C = 20.0, 40.0
IBI_MIN_MS, IBI_MAX_MS = 400, 1600      # 37–150 bpm pendant le sommeil


def jget(s) -> dict | None:
    """decoded_json \u2192 dict (None si absent, invalide ou pas un objet)."""
    if not s:
        return None
    if isinstance(s, dict):
        return s
    try:
        v = json.loads(s)
    except (TypeError, ValueError):
        return None
    return v if isinstance(v, dict) else None


def rows(con, sql, args=()):
    return con.execute(sql, args).fetchall()


def median(v):
    return statistics.median(v) if v else None


def mean(v):
    return sum(v) / len(v) if v else None


def clamp(x, lo, hi):
    return max(lo, min(hi, x))


def lerp_score(x: float, pts) -> float:
    """Interpolation linéaire par morceaux sur [(x, score), …] (x croissants),
    bornée aux extrémités. Sert à tous les contributeurs 0–100."""
    if x <= pts[0][0]:
        return float(pts[0][1])
    for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
        if x <= x1:
            return y0 + (y1 - y0) * (x - x0) / (x1 - x0)
    return float(pts[-1][1])


def rating(score):
    """Paliers officiels Oura pour les scores et contributeurs."""
    if score is None:
        return None
    if score >= 85:
        return "optimal"
    if score >= 70:
        return "bon"
    if score >= 60:
        return "moyen"
    return "attention"


# ---------------------------------------------------------------- axe horaire

EPOCH_SLACK_DS = 6 * 3600 * 10   # port de open_oura tools/epoch_time.py
DRIFT_TOL_S = 900                # écart hôte BLE ↔ serveur toléré sans correction
FIT_HALF_WINDOW_S = 24 * 3600    # fenêtre de l'ajustement local des ancres
FIT_MIN_ANCHORS = 6
KNOT_STEP_S = 1800


def build_epochs(pairs):
    """Epochs de démarrage : rt croissant, coupure sur recul > 6 h. Chaque epoch
    = [rt_min, rt_max, cap_du_rt_max, cap_min, cap_max]."""
    order = sorted((cu, ds) for ds, cu in pairs if ds is not None and cu is not None)
    epochs = []
    for cu, ds in order:
        if epochs and ds >= epochs[-1][1] - EPOCH_SLACK_DS:
            e = epochs[-1]
            if ds >= e[1]:
                e[1] = ds
                e[2] = cu
            e[0] = min(e[0], ds)
            e[4] = cu
        else:
            epochs.append([ds, ds, cu, cu, cu])
    return epochs


def _wls(xs, ys, ws):
    """Régression linéaire pondérée → (a, b) avec y = a + b·x."""
    sw = sum(ws)
    if sw <= 0:
        return None
    mx = sum(w * x for x, w in zip(xs, ws)) / sw
    my = sum(w * y for y, w in zip(ys, ws)) / sw
    sxx = sum(w * (x - mx) ** 2 for x, w in zip(xs, ws))
    if sxx <= 1e-9:
        return my, 0.0
    b = sum(w * (x - mx) * (y - my) for x, y, w in zip(xs, ys, ws)) / sxx
    return my - b * mx, b


def _robust_local_offset(anchors, k):
    """Décalage (unix − rt/10) estimé au point k (s d'anneau) : régression
    locale tricube, une passe de repondération bicarrée contre les ancres
    aberrantes. Les ancres time_sync sont bruitées (±100–200 s) alors que
    l'horloge de l'anneau est stable (~130 ppm) : lisser est légitime."""
    near = sorted(anchors, key=lambda a: abs(a[0] - k))
    sel = [a for a in near if abs(a[0] - k) <= FIT_HALF_WINDOW_S]
    if len(sel) < FIT_MIN_ANCHORS:
        sel = near[:FIT_MIN_ANCHORS]
    if len(sel) == 1:
        return sel[0][1]
    span = max(abs(a[0] - k) for a in sel) * 1.2 + 1.0
    xs = [a[0] - k for a in sel]
    ys = [a[1] for a in sel]
    ws = [(1 - (abs(x) / span) ** 3) ** 3 for x in xs]
    fit = _wls(xs, ys, ws)
    if fit is None:
        return statistics.median(ys)
    a, b = fit
    res = [y - (a + b * x) for x, y in zip(xs, ys)]
    mad = statistics.median(abs(r) for r in res) or 1.0
    rw = [(1 - (r / (6 * mad)) ** 2) ** 2 if abs(r) < 6 * mad else 0.0 for r in res]
    fit2 = _wls(xs, ys, [w * r for w, r in zip(ws, rw)])
    return (fit2 or fit)[0]


class TimeAxis:
    """Mappe un ring_timestamp (ds depuis le démarrage) sur l'heure unix.

    - Par epoch de démarrage : ancres time_sync (tag 66) lissées par régression
      locale robuste → courbe monotone, interpolée entre nœuds de 30 min.
      Avant la première ancre d'un epoch, extrapolation par le premier nœud
      (l'horloge de l'anneau dérive de ~0,5 s/h).
    - Epoch sans ancre : ancrage sur l'heure de capture (port epoch_time.py).
    - L'ancre porte l'horloge de l'hôte BLE : si elle s'écarte de plus de
      15 min de l'heure de capture du dernier événement, l'axe est recalé.

    mode ∈ {"sync", "epoch", "capture"} (celui de l'epoch le plus récent).
    """

    def __init__(self, con):
        pairs = rows(con, "SELECT ring_timestamp, captured_unix FROM events")
        self.epochs = build_epochs(pairs)
        anchors = []
        for rt, dj, cap in rows(con, "SELECT ring_timestamp, decoded_json, captured_unix"
                                     " FROM events WHERE tag=? ORDER BY ring_timestamp",
                                (T_TIME_SYNC,)):
            j = jget(dj)
            u = j.get("unix_time") if j else None
            if isinstance(u, (int, float)) and u > 1_500_000_000:
                anchors.append((rt, float(u), cap))
        # modèle par epoch : nœuds (x = rt/10, t) croissants
        per = {}
        for rt, u, cap in anchors:
            i = self._epoch_i(rt, cap)
            if i is not None:
                per.setdefault(i, []).append((rt / 10.0, u - rt / 10.0))
        self.models = [self._fit(e, per[i]) if i in per else None
                       for i, e in enumerate(self.epochs)]
        m = self.models[-1] if self.models else None
        self.mode = "sync" if m else ("epoch" if self.epochs else "capture")
        self.first_anchor_ds = anchors[0][0] if anchors else None
        self.drift_s = 0.0
        last = con.execute("SELECT ring_timestamp, captured_unix FROM events"
                           " ORDER BY id DESC LIMIT 1").fetchone()
        if last and m:
            t = self._raw(last[0], last[1])
            if t is not None and abs(t - last[1]) > DRIFT_TOL_S:
                self.drift_s = t - last[1]

    def _epoch_i(self, rt, cap):
        cands = [i for i, e in enumerate(self.epochs)
                 if e[0] - EPOCH_SLACK_DS <= rt <= e[1] + EPOCH_SLACK_DS]
        if not cands:
            return len(self.epochs) - 1 if self.epochs else None
        if len(cands) == 1 or cap is None:
            return cands[-1]

        def dist(i):
            e = self.epochs[i]
            return 0 if e[3] <= cap <= e[4] else min(abs(cap - e[3]), abs(cap - e[4]))
        return min(cands, key=dist)

    @staticmethod
    def _fit(epoch, anchors):
        x0, x1 = epoch[0] / 10.0, epoch[1] / 10.0
        knots_x = []
        x = x0
        while x < x1:
            knots_x.append(x)
            x += KNOT_STEP_S
        knots_x.append(x1)
        knots_t = [kx + _robust_local_offset(anchors, kx) for kx in knots_x]
        for i in range(1, len(knots_t)):          # monotonie stricte garantie
            if knots_t[i] < knots_t[i - 1]:
                knots_t[i] = knots_t[i - 1]
        return knots_x, knots_t

    def _raw(self, rt, cap=None):
        if rt is None:
            return None
        i = self._epoch_i(rt, cap)
        if i is None:
            return cap
        e, m = self.epochs[i], self.models[i]
        if m is None:
            return e[2] - (e[1] - rt) / 10.0
        kx, kt = m
        x = rt / 10.0
        if x <= kx[0]:
            return kt[0] + (x - kx[0])
        if x >= kx[-1]:
            return kt[-1] + (x - kx[-1])
        i = bisect.bisect_right(kx, x) - 1
        f = (x - kx[i]) / (kx[i + 1] - kx[i])
        return kt[i] + f * (kt[i + 1] - kt[i])

    def __call__(self, rt, cap=None):
        t = self._raw(rt, cap)
        return None if t is None else t - self.drift_s

    def to_ds(self, unix):
        """Inverse approché (epoch le plus récent) : unix → rt."""
        if not self.epochs:
            return None
        e = self.epochs[-1]
        m = self.models[-1]
        if m is None:
            return int(e[1] - (e[2] - unix) * 10)
        kx, kt = m
        u = unix + self.drift_s
        if u <= kt[0]:
            x = kx[0] + (u - kt[0])
        elif u >= kt[-1]:
            x = kx[-1] + (u - kt[-1])
        else:
            i = bisect.bisect_right(kt, u) - 1
            span = kt[i + 1] - kt[i]
            x = kx[i] + ((u - kt[i]) / span if span > 0 else 0) * (kx[i + 1] - kx[i])
        return int(round(x * 10))


# ---------------------------------------------------------------- état de l'anneau

STATE_LABELS = {
    "chg. detected": "sur le chargeur",
    "chg. stopped": "retiré du chargeur",
    "hr enable": "mesure de FC active",
    "motion det": "mouvement détecté",
    "timeout": "au repos",
    "det. timeout": "au repos",
    "fea off": "capteurs en veille",
    "orientation": "changement d'orientation",
    "activity": "activité détectée",
    "HW test start": "auto-test matériel",
    "HW test end": "auto-test matériel terminé",
    "fld tst end": "auto-test terminé",
}


def skin_temp(decoded):
    """temp_event (tag 70) porte 3 capteurs : [0] peau (≈ sleep_temp_event),
    [1] quantifié au degré, [2] ~2 °C plus chaud (interne). Seul [0] est une
    température cutanée ; None hors bornes physiologiques."""
    v = (decoded or {}).get("temps_c") or []
    if not v:
        return None
    t = v[0]
    return t if SKIN_TEMP_MIN_C <= t <= SKIN_TEMP_MAX_C else None


def state_label(tag, state, text):
    text = (text or "").strip()
    if text in STATE_LABELS:
        return STATE_LABELS[text]
    if text.isdigit():
        if tag == T_WEAR and state == 3:
            return "anneau porté (code interne %s)" % text
        return "code interne %s (état %s)" % (text, state)
    return (text or "état %s" % state)


def charging_intervals_ds(con):
    """Périodes sur le chargeur en temps anneau : de « chg. detected » au
    « chg. stopped » suivant (plafond 4 h si l'arrêt manque)."""
    ev = []
    for tag, rt, dj in rows(con, "SELECT tag, ring_timestamp, decoded_json FROM events"
                                 " WHERE tag IN (?,?) ORDER BY ring_timestamp", (T_STATE, T_WEAR)):
        j = jget(dj) or {}
        t = j.get("text")
        if t in ("chg. detected", "chg. stopped"):
            ev.append((rt, t))
    out, start = [], None
    for rt, t in ev:
        if t == "chg. detected":
            if start is None:
                start = rt
        elif start is not None:
            out.append((start, rt))
            start = None
    if start is not None:
        out.append((start, start + 4 * 36000))
    return out


def in_intervals(x, intervals):
    return any(a <= x <= b for a, b in intervals)


# ---------------------------------------------------------------- nuits

def load_nights(con, axis, min_h=3.0, max_h=16.0):
    """Fenêtres de sommeil issues de bedtime_period (analyse embarquée),
    dédupliquées : révisions successives d'une même nuit (chevauchement > 50 %),
    la plus récente l'emporte."""
    from datetime import datetime
    raw = []
    for rt, dj in rows(con, "SELECT ring_timestamp, decoded_json FROM events"
                            " WHERE tag=? ORDER BY ring_timestamp", (T_BEDTIME,)):
        j = jget(dj)
        if not j:
            continue
        s, e = j.get("bedtime_start_ds"), j.get("bedtime_end_ds")
        if not s or not e or not (min_h * 36000 <= e - s <= max_h * 36000):
            continue
        su, eu = axis(s), axis(e)
        if su is None or eu is None or eu <= su:
            continue
        raw.append({"start_ds": s, "end_ds": e, "ref_ds": rt,
                    "start_unix": su, "end_unix": eu})
    out = []
    for r in sorted(raw, key=lambda r: r["ref_ds"]):
        for o in out:
            ov = min(r["end_ds"], o["end_ds"]) - max(r["start_ds"], o["start_ds"])
            span = max(r["end_ds"] - r["start_ds"], o["end_ds"] - o["start_ds"])
            if ov > 0 and span > 0 and ov / span > 0.5:
                o.update(r)
                break
        else:
            out.append(dict(r))
    for r in out:
        r["night"] = datetime.fromtimestamp(r["end_unix"]).strftime("%Y-%m-%d")
    # une nuit par date : la plus longue
    best = {}
    for r in out:
        b = best.get(r["night"])
        if b is None or (r["end_ds"] - r["start_ds"]) > (b["end_ds"] - b["start_ds"]):
            best[r["night"]] = r
    return [best[k] for k in sorted(best)]


# ---------------------------------------------------------------- séries cardiaques

def hrv_bins(con, s_ds, e_ds):
    """Bins de 5 min calculés par l'anneau (hrv_event) dont le CENTRE tombe
    dans [s_ds, e_ds). Renvoie [(centre_ds, hr_bpm|None, rmssd_ms|None)].
    Les bins 0/0 (pas de mesure) et hors bornes physiologiques → None."""
    out = []
    step = HRV_BIN_S * 10
    for rt, dj in rows(con, "SELECT ring_timestamp, decoded_json FROM events WHERE tag=?"
                            " AND ring_timestamp>=? AND ring_timestamp<? ORDER BY ring_timestamp",
                       (T_HRV, s_ds, e_ds + 12 * step)):
        j = jget(dj) or {}
        hr, rm = j.get("hr_bpm") or [], j.get("rmssd_ms") or []
        n = min(len(hr), len(rm))
        for i in range(n):
            c = rt - (n - i - 0.5) * step
            if not (s_ds <= c < e_ds):
                continue
            h = hr[i] if HR_MIN_BPM <= (hr[i] or 0) <= HR_MAX_BPM else None
            r = rm[i] if RMSSD_MIN_MS <= (rm[i] or 0) <= RMSSD_MAX_MS else None
            out.append((c, h, r))
    out.sort()
    # dédoublonnage (événements rejoués)
    ded = []
    for b in out:
        if ded and abs(b[0] - ded[-1][0]) < step / 2:
            continue
        ded.append(b)
    return ded


def load_beats(con, s_ds, e_ds, regap_ds=50):
    """Battements (tag 96, IBI + amplitude) sur [s_ds, e_ds) : liste de
    (t_ds, ibi_ms), t = instant du battement qui clôt l'intervalle.

    Les paquets de 6 IBI sont contigus (Σ IBI ≈ écart moyen entre paquets)
    mais horodatés à des instants arrondis (écarts alternés 4,5 / 6,8 s) : le
    temps est reconstruit par somme cumulée des IBI, et réancré sur rt
    seulement quand l'écart dépasse 5 s (trou réel dans le flux)."""
    beats = []
    t = None
    for rt, dj in rows(con, "SELECT ring_timestamp, decoded_json FROM events WHERE tag=?"
                            " AND ring_timestamp>=? AND ring_timestamp<? ORDER BY ring_timestamp",
                       (T_IBI, s_ds, e_ds)):
        j = jget(dj) or {}
        ib = [x for x in (j.get("ibi_ms") or []) if x]
        if not ib:
            continue
        span = sum(ib) / 100.0      # ms → ds
        if t is None or abs((t + span) - rt) > regap_ds:
            t = rt - span
        for x in ib:
            t += x / 100.0
            beats.append((t, x))
    return beats


def clean_beats(beats, max_rel_jump=0.2):
    """Filtre d'artefacts : bornes physiologiques + écart relatif au battement
    accepté précédent ≤ 20 % (règle classique de correction d'IBI)."""
    out = []
    for t, x in beats:
        if not IBI_MIN_MS <= x <= IBI_MAX_MS:
            continue
        if out and abs(x - out[-1][1]) > max_rel_jump * out[-1][1] and t - out[-1][0] < 50:
            continue
        out.append((t, x))
    return out


def rmssd(ibis):
    if len(ibis) < 3:
        return None
    d = [(b - a) ** 2 for a, b in zip(ibis, ibis[1:])]
    return math.sqrt(sum(d) / len(d))


def _welch_peak(ts, xs, fs=2.0, seg_s=120, f_lo=0.15, f_hi=0.5):
    """Fréquence du pic spectral (Hz) d'une série irrégulière (t en s) :
    rééchantillonnage linéaire à fs, segments de seg_s à 50 %, fenêtre de Hann,
    DFT restreinte à la bande [f_lo, f_hi]. Renvoie (f_pic, netteté)."""
    if len(ts) < 10 or ts[-1] - ts[0] < seg_s:
        return None, 0.0
    n = int((ts[-1] - ts[0]) * fs)
    grid = [ts[0] + i / fs for i in range(n)]
    ys, j = [], 0
    for g in grid:
        while j + 1 < len(ts) and ts[j + 1] < g:
            j += 1
        if j + 1 >= len(ts):
            ys.append(xs[-1])
            continue
        t0, t1 = ts[j], ts[j + 1]
        f = (g - t0) / (t1 - t0) if t1 > t0 else 0.0
        ys.append(xs[j] + f * (xs[j + 1] - xs[j]))
    L = int(seg_s * fs)
    freqs = []
    k = 1
    while k / (L / fs) <= f_hi:
        f = k / (L / fs)
        if f >= f_lo:
            freqs.append((k, f))
        k += 1
    hann = [0.5 - 0.5 * math.cos(2 * math.pi * i / (L - 1)) for i in range(L)]
    tables = [([math.cos(2 * math.pi * k * i / L) for i in range(L)],
               [math.sin(2 * math.pi * k * i / L) for i in range(L)]) for k, _ in freqs]
    power = [0.0] * len(freqs)
    segs = 0
    for s0 in range(0, len(ys) - L + 1, L // 2):
        seg = ys[s0:s0 + L]
        m = sum(seg) / L
        seg = [(v - m) * w for v, w in zip(seg, hann)]
        for fi, (cs, sn) in enumerate(tables):
            re = sum(v * c for v, c in zip(seg, cs))
            im = sum(v * s for v, s in zip(seg, sn))
            power[fi] += re * re + im * im
        segs += 1
    if not segs or not freqs:
        return None, 0.0
    i = max(range(len(power)), key=lambda q: power[q])
    tot = sum(power) or 1.0
    return freqs[i][1], power[i] / tot


def resp_rate_windows(clean, s_ds, e_ds, win_s=300, min_cover=0.8):
    """Fréquence respiratoire (cycles/min) par fenêtres de 5 min, à partir de
    l'arythmie sinusale respiratoire (méthode décrite par Oura : la FC monte à
    l'inspiration, baisse à l'expiration). Fenêtres rejetées si couverture en
    battements propres < 80 % ou pic spectral peu net. EXPÉRIMENTAL : non
    validé contre une référence sur cet anneau."""
    out = []
    ts_all = [t for t, _ in clean]
    step = win_s * 10
    a = s_ds
    while a + step <= e_ds:
        i0 = bisect.bisect_left(ts_all, a)
        i1 = bisect.bisect_left(ts_all, a + step)
        seg = clean[i0:i1]
        cover = sum(x for _, x in seg) / 1000.0 / win_s
        if len(seg) >= 60 and cover >= min_cover:
            f, sharp = _welch_peak([t / 10.0 for t, _ in seg], [float(x) for _, x in seg])
            if f and sharp >= 0.12:
                out.append((a + step / 2, f * 60.0))
        a += step
    return out


# ---------------------------------------------------------------- activité

def activity_minutes(con, axis, t0, t1):
    """Bins d'une minute d'activity_information (tag 80) entre t0 et t1 (unix),
    alignés sur la FIN de l'événement : {minute_unix: {"met": float, "steps": float}}.

    Pas : l'octet d'en-tête de l'événement (décodé « state » par open_oura) suit
    l'intensité minute par minute (r = 0,955 avec le MET moyen, 192 valeurs
    distinctes, 0 au repos ; open_oura data-recovery-map : « MET-level bins +
    step counts »). C'est très probablement le nombre de pas de la fenêtre :
    HYPOTHÈSE non validée par une marche comptée, affichée comme telle. Il est
    réparti sur les minutes de l'événement au prorata de (MET − 1)+, ou
    uniformément si toutes sont au repos."""
    out = {}
    s_ds = axis.to_ds(t0 - 3600)
    e_ds = axis.to_ds(t1 + 3600)
    for rt, cap, dj in rows(con, "SELECT ring_timestamp, captured_unix, decoded_json FROM events"
                                 " WHERE tag=? AND decoded_json IS NOT NULL AND ring_timestamp>=?"
                                 " AND ring_timestamp<? ORDER BY ring_timestamp",
                            (T_ACTIVITY, s_ds or 0, e_ds or 1 << 62)):
        j = jget(dj) or {}
        met = j.get("met") or []
        n = len(met)
        end = axis(rt, cap)
        if end is None or not n:
            continue
        steps = j.get("state")
        steps = float(steps) if isinstance(steps, (int, float)) and 0 <= steps <= 255 else 0.0
        w = [max(0.0, (v or 0.0) - 1.0) for v in met]
        tot = sum(w)
        for i, v in enumerate(met):
            t = end - (n - i) * MET_BIN_S
            if t0 <= t < t1 and v is not None:
                share = steps * (w[i] / tot if tot > 0 else 1.0 / n)
                out[int(t // 60) * 60] = {"met": float(v), "steps": share}
    return out


def met_minutes(con, axis, t0, t1):
    """Bins MET d'une minute entre t0 et t1 (unix), alignés sur la fin de
    l'événement : {minute_unix: met}."""
    return {t: d["met"] for t, d in activity_minutes(con, axis, t0, t1).items()}


def bmr_kcal(age, height_cm, weight_kg, sex):
    """Métabolisme de base (Mifflin-St Jeor, 1990), kcal par jour ; None si le
    profil est incomplet. Sexe non précisé : moyenne des deux constantes."""
    if not (age and height_cm and weight_kg):
        return None
    base = 10 * float(weight_kg) + 6.25 * float(height_cm) - 5 * float(age)
    return base + {"male": 5.0, "female": -161.0}.get(sex, -78.0)


def day_hr_minutes(con, axis, t0, t1, min_beats=5):
    """FC de jour : mesures de l'anneau à la LED verte (tag 128, suspendues
    pendant le sommeil). Médiane par minute des FC de bonne qualité (l'anneau ne
    renseigne hr_bpm que pour les battements de qualité 1), si la minute en
    compte au moins min_beats : {minute_unix: bpm}."""
    s_ds, e_ds = axis.to_ds(t0 - 600), axis.to_ds(t1 + 600)
    acc = {}
    for rt, cap, dj in rows(con, "SELECT ring_timestamp, captured_unix, decoded_json FROM events"
                                 " WHERE tag=? AND ring_timestamp>=? AND ring_timestamp<?",
                            (T_GREEN_IBI, s_ds or 0, e_ds or 1 << 62)):
        t = axis(rt, cap)
        if t is None or not t0 <= t < t1:
            continue
        hrs = [b for b in ((jget(dj) or {}).get("hr_bpm") or []) if HR_MIN_BPM <= b <= HR_MAX_BPM]
        if hrs:
            acc.setdefault(int(t // 60) * 60, []).extend(hrs)
    return {m: statistics.median(v) for m, v in sorted(acc.items()) if len(v) >= min_beats}


def rest_periods(con, axis, nights, max_h=3.0):
    """Périodes de repos détectées par l'anneau hors des nuits : bedtime_period
    de moins de 3 h, révisions dédoublonnées (même début à 2 min près → la plus
    récente), périodes chevauchant une nuit retirées (ce sont des révisions
    précoces de la nuit). → [{"start_ds", "end_ds", "start_unix", "end_unix"}]."""
    ded = {}
    for rt, dj in rows(con, "SELECT ring_timestamp, decoded_json FROM events"
                            " WHERE tag=? ORDER BY ring_timestamp", (T_BEDTIME,)):
        j = jget(dj) or {}
        s, e = j.get("bedtime_start_ds"), j.get("bedtime_end_ds")
        if not (s and e and 0 < e - s < max_h * 36000):
            continue
        k = next((k for k in ded if abs(k - s) <= 1200), s)
        if k not in ded or rt >= ded[k][2]:
            ded[k] = (s, e, rt)
    out = []
    for s, e, _ in sorted(ded.values()):
        if any(s < n["end_ds"] and e > n["start_ds"] for n in nights):
            continue
        su, eu = axis(s), axis(e)
        if su and eu and eu > su:
            out.append({"start_ds": s, "end_ds": e, "start_unix": su, "end_unix": eu})
    return out


# ---------------------------------------------------------------- journée d'activité

# Journée d'activité de 04:00 à 03:59 (Oura : « activity period is from 4 AM to
# 3:59 AM ») : une soirée tardive reste sur la bonne journée.
ACT_DAY_START_H = 4
# Classes d'intensité (aide Oura actuelle, « How Oura Measures Steps & Activity ») :
# repos < 1,05 MET (allongé, immobile : ni actif ni sédentaire), sédentaire
# 1,05–1,5, léger 1,5–3, modéré 3–6, intense ≥ 6 ; calories actives ≥ 1,5 MET.
MET_REST, MET_LIGHT, MET_MODERATE, MET_VIGOROUS = 1.05, 1.5, 3.0, 6.0


def activity_day_bounds(day):
    """'AAAA-MM-JJ' → (unix 04:00 du jour, unix 04:00 du lendemain), heure locale."""
    from datetime import datetime, timedelta
    d = datetime.strptime(day, "%Y-%m-%d")
    return ((d + timedelta(hours=ACT_DAY_START_H)).timestamp(),
            (d + timedelta(days=1, hours=ACT_DAY_START_H)).timestamp())


def activity_day(unix):
    """Journée d'activité contenant l'instant unix (avant 04:00 → la veille)."""
    from datetime import datetime, timedelta
    return (datetime.fromtimestamp(unix) - timedelta(hours=ACT_DAY_START_H)).strftime("%Y-%m-%d")


def wear_removals(con, axis):
    """Retraits signalés par l'anneau : wear_event (tag 83) état 1 → événement
    83 suivant (état 3 « porté » ou 8 « charge ») ; [(t0, t1)] unix."""
    ev = [(axis(rt, cap), (jget(dj) or {}).get("state"))
          for rt, cap, dj in rows(con, "SELECT ring_timestamp, captured_unix, decoded_json FROM events"
                                       " WHERE tag=? ORDER BY ring_timestamp", (T_WEAR,))]
    ev = [(t, st) for t, st in ev if t is not None]
    return [(a, b) for (a, s), (b, _) in zip(ev, ev[1:]) if s == 1 and b > a]


def skin_hot_minutes(con, axis, t0, t1, charging_ds=()):
    """Minutes où la peau dépasse 30 °C à ±5 min (capteur cutané, hors charge)."""
    s_ds, e_ds = axis.to_ds(t0 - 600), axis.to_ds(t1 + 600)
    hot = set()
    for rt, cap, dj in rows(con, "SELECT ring_timestamp, captured_unix, decoded_json FROM events"
                                 " WHERE tag=? AND ring_timestamp>=? AND ring_timestamp<?",
                            (T_TEMP, s_ds or 0, e_ds or 1 << 62)):
        if in_intervals(rt, charging_ds):
            continue
        v = skin_temp(jget(dj))
        if v is not None and v > 30.0:
            t = axis(rt, cap)
            if t is not None:
                m = int(t // 60)
                hot.update(k * 60 for k in range(m - 5, m + 6))
    return hot


def motion_minutes(con, axis, t0, t1):
    """Secondes en mouvement par minute (motion_event, tag 71) : {minute: s}."""
    s_ds, e_ds = axis.to_ds(t0 - 600), axis.to_ds(t1 + 600)
    out = {}
    for rt, cap, dj in rows(con, "SELECT ring_timestamp, captured_unix, decoded_json FROM events"
                                 " WHERE tag=? AND ring_timestamp>=? AND ring_timestamp<?",
                            (T_MOTION, s_ds or 0, e_ds or 1 << 62)):
        t = axis(rt, cap)
        if t is not None and t0 <= t < t1:
            m = int(t // 60) * 60
            out[m] = out.get(m, 0) + ((jget(dj) or {}).get("motion_seconds") or 0)
    return out


def _within(m, windows):
    return any(a <= m < b for a, b in windows)


def activity_grid(con, axis, t0, t1, nights=(), rests=(), charges=(), removals=(), charging_ds=()):
    """Grille minute par minute de [t0, t1) : {minute: {"state", "met", "steps"}}.

    États, par priorité : « charge », « retiré » (wear_event), « lit » (fenêtre
    de nuit), « repos » (repos de jour détecté par l'anneau), « non porté »,
    « éveil ». Une minute hors de ces fenêtres est portée si la peau dépasse
    30 °C à ±5 min, OU si l'anneau mesure un mouvement (tag 71), OU une
    intensité ≥ 1,5 MET : la seule règle de température classait « non porté »
    des minutes actives aux doigts froids (171 min sur la période auditée).
    met / steps : bins d'activity_information (None si la minute n'en a pas)."""
    def clip(ws):
        return [(a, b) for a, b in ws if a is not None and b is not None and b > t0 and a < t1]
    nights, rests, charges, removals = clip(nights), clip(rests), clip(charges), clip(removals)
    am = activity_minutes(con, axis, t0, t1)
    hot = skin_hot_minutes(con, axis, t0, t1, charging_ds)
    mov = motion_minutes(con, axis, t0, t1)
    grid = {}
    m = int(t0 // 60) * 60
    while m < t1:
        a = am.get(m)
        met = a["met"] if a else None
        if _within(m, charges):
            st = "charge"
        elif _within(m, removals):
            st = "retiré"
        elif _within(m, nights):
            st = "lit"
        elif _within(m, rests):
            st = "repos"
        elif m in hot or mov.get(m, 0) > 0 or (met is not None and met >= MET_LIGHT):
            st = "éveil"
        else:
            st = "non porté"
        grid[m] = {"state": st, "met": met, "steps": a["steps"] if a else 0.0, "motion": mov.get(m, 0)}
        m += 60
    return grid


def grid_runs(grid, states):
    """Intervalles contigus [(début, fin)] des minutes dans `states`."""
    out = []
    for m in sorted(grid):
        if grid[m]["state"] in states:
            if out and out[-1][1] == m:
                out[-1][1] = m + 60
            else:
                out.append([m, m + 60])
    return [tuple(x) for x in out]


# ---------------------------------------------------------------- batterie

def battery_points(con, axis, since_unix=None):
    """Historique du niveau de batterie : [(t, pct, mv | None)] trié.
    Sources : debug_data « battery_level_changed » (0x24 : % et mV) et traces
    firmware « batt: N » (debug_event, plus denses, % seulement)."""
    pts = []
    s_ds = (axis.to_ds(since_unix - 3600) or 0) if since_unix is not None else 0
    for rt, cap, dj in rows(con, "SELECT ring_timestamp, captured_unix, decoded_json FROM events"
                                 " WHERE tag=? AND ring_timestamp>=? AND decoded_json LIKE '%battery_level_changed%'",
                            (T_DEBUG_DATA, s_ds)):
        j = jget(dj) or {}
        t, p = axis(rt, cap), j.get("battery_pct")
        if t is not None and isinstance(p, (int, float)) and 0 <= p <= 100:
            pts.append((t, int(p), j.get("voltage_mv")))
    for rt, cap, dj in rows(con, "SELECT ring_timestamp, captured_unix, decoded_json FROM events"
                                 " WHERE tag=? AND ring_timestamp>=? AND decoded_json LIKE '%batt:%'",
                            (T_DEBUG_EVENT, s_ds)):
        a = (jget(dj) or {}).get("ascii") or ""
        try:
            p = int(a.split("batt:")[1].split()[0])
        except (IndexError, ValueError):
            continue
        t = axis(rt, cap)
        if t is not None and 0 <= p <= 100:
            pts.append((t, p, None))
    pts.sort(key=lambda x: x[0])
    if since_unix is not None:
        pts = [x for x in pts if x[0] >= since_unix]
    return pts


def discharge_rate(points, charges, min_span_s=6 * 3600):
    """Pente de décharge (%/h, positive) par régression linéaire sur chaque
    segment entre deux charges (30 min après la fin de charge, le temps que la
    mesure se stabilise). → (pente du segment en cours ou None, pente typique
    = moyenne des segments pondérée par leur durée, ou None)."""
    if not points:
        return None, None
    edges = [points[0][0]] + [x for c in charges for x in c] + [points[-1][0] + 1]
    rates = []
    for a, b in zip(edges[0::2], edges[1::2]):
        seg = [(t, p) for t, p, _ in points if a + 1800 <= t <= b]
        if len(seg) < 5 or seg[-1][0] - seg[0][0] < min_span_s:
            rates.append(None)
            continue
        n = len(seg)
        mt = sum(t for t, _ in seg) / n
        mp = sum(p for _, p in seg) / n
        den = sum((t - mt) ** 2 for t, _ in seg)
        slope = sum((t - mt) * (p - mp) for t, p in seg) / den if den else 0.0
        rates.append((-slope * 3600, seg[-1][0] - seg[0][0]))
    ok = [r for r in rates if r and r[0] > 0]
    typical = sum(r * w for r, w in ok) / sum(w for _, w in ok) if ok else None
    current = rates[-1][0] if rates and rates[-1] and rates[-1][0] > 0 else None
    return current, typical
