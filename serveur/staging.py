"""staging — hypnogramme estimé (30 s) à partir du mouvement et des battements.

Heuristique OUVERTE, non validée contre polysomnographie. Principes (repris de
la littérature sur la stadification par PPG + accéléromètre, dont Altini &
Kinnunen 2021 pour l'anneau Oura : mouvement, variabilité cardiaque et
caractéristique circadienne) :

- caractéristiques par epoch, lissées sur ~5 min et normalisées sur la nuit
  (z robuste) : FC, irrégularité de la FC (écart-type des FC d'epoch), RMSSD,
  mouvement ;
- sommeil profond : FC basse et régulière, HRV haute, immobilité — plus
  fréquent en début de nuit ;
- REM : FC plus haute et irrégulière, immobilité (atonie) — quasi absent dans
  la première heure, plus fréquent en seconde moitié ;
- éveil : mouvement franc, ou FC haute + HRV basse ;
- lissage par Viterbi (HMM 4 états, forte persistance) : des stades de 30 s
  qui alternent sans cesse ne sont pas physiologiques.
"""
import bisect
import math
import statistics

EPOCH_DS = 300                  # 30 s
STATES = ("awake", "light", "deep", "rem")
MOTION_AWAKE = 2.0              # MAD tag 114 : > 2 = mouvement franc (p90 nuit ≈ 0,3)
MOTION_RESTLESS = 0.5

# Émissions gaussiennes sur (z FC, z irrégularité de la FC) : (moyennes), (écarts-types).
# Le RMSSD n'est pas retenu : sur cet anneau il suit surtout les artefacts de
# mouvement (corrélé positivement à la FC dans les nuits observées).
EMIT = {
    "awake": ((1.5, 1.0), (1.2, 1.2)),
    "light": ((0.0, 0.0), (1.0, 1.0)),
    "deep": ((-0.8, -0.8), (0.7, 0.6)),
    "rem": ((0.5, 0.8), (0.9, 0.9)),
}
LOG_PRIOR = {"awake": -1.5, "light": 0.0, "deep": -0.3, "rem": 0.0}
# Plafonds physiologiques (part du sommeil total) : au-delà, l'a priori du stade
# est abaissé par dichotomie pour CETTE nuit. Garde-fou contre des valeurs
# impossibles, pas un recalage vers la norme (NSF adulte : N3 16–20 %, REM 21–30 %).
CAPS = {"deep": 0.25, "rem": 0.30}
# planchers d'échelle des z robustes : une FC très stable (MAD ~1 bpm)
# gonflerait sinon des écarts insignifiants
Z_FLOOR = {"hr": 2.5, "irr": 0.8}
P_STAY = 0.975
PAIR_PENALTY = {("deep", "rem"): -2.0, ("rem", "deep"): -2.0}


def _robust_z(v, floor=0.0):
    xs = [x for x in v if x is not None]
    if len(xs) < 10:
        return [0.0 if x is not None else None for x in v]
    med = statistics.median(xs)
    scale = max(statistics.median(abs(x - med) for x in xs) * 1.4826, floor) or 1.0
    return [None if x is None else (x - med) / scale for x in v]


def _roll(v, half, fn):
    n = len(v)
    out = []
    for i in range(n):
        w = [x for x in v[max(0, i - half):min(n, i + half + 1)] if x is not None]
        out.append(fn(w) if w else None)
    return out


def epoch_features(n, s_ds, motion_events, beats):
    """motion_events : [(rt_fin, mad)] ; beats : [(t_ds, ibi_ms)] propres."""
    motion = [0.0] * n
    for rt, m in motion_events:
        i = int((rt - EPOCH_DS / 2 - s_ds) // EPOCH_DS)
        if 0 <= i < n:
            motion[i] = max(motion[i], m)
    bt = [t for t, _ in beats]
    hr_e, ibis_e = [], []
    for i in range(n):
        a = bisect.bisect_left(bt, s_ds + i * EPOCH_DS)
        b = bisect.bisect_left(bt, s_ds + (i + 1) * EPOCH_DS)
        ib = [x for _, x in beats[a:b]]
        ibis_e.append(ib)
        hr_e.append(60000.0 / (sum(ib) / len(ib)) if len(ib) >= 10 else None)
    hr_s = _roll(hr_e, 5, statistics.median)
    irr = _roll(hr_e, 5, lambda w: statistics.pstdev(w) if len(w) >= 4 else None)
    mot_s = _roll(motion, 2, max)
    return {"motion": motion, "motion_s": mot_s, "hr": hr_e, "hr_s": hr_s,
            "z_hr": _robust_z(hr_s, Z_FLOOR["hr"]), "z_irr": _robust_z(irr, Z_FLOOR["irr"])}


def _emission(st, zf, mot, mot_s, u_min, frac, bias=None):
    mus, sds = EMIT[st]
    s = LOG_PRIOR[st] + (bias or {}).get(st, 0.0)
    for z, mu, sd in zip(zf, mus, sds):
        if z is not None:
            z = max(-4.0, min(4.0, z))
            s -= (z - mu) ** 2 / (2 * sd * sd) + math.log(sd)
    # mouvement : un mouvement franc exclut l'atonie du REM et le sommeil profond
    if mot > MOTION_AWAKE:
        s += {"awake": 3.0, "light": -1.0, "deep": -2.5, "rem": -2.5}[st]
    elif mot_s is not None and mot_s > MOTION_RESTLESS:
        s += {"awake": 0.5, "light": 0.2, "deep": -1.0, "rem": -0.5}[st]
    # a priori circadiens / architecture
    if st == "rem":
        if u_min < 60:
            s -= 5.0 * (1 - u_min / 60.0) + 0.5
        elif frac > 0.5:
            s += 0.4
    if st == "deep":
        if frac < 0.4:
            s += 0.4
        elif frac > 0.7:
            s -= 0.8
    return s


def viterbi(feats, n, bias=None):
    ls, lsw = math.log(P_STAY), math.log((1 - P_STAY) / 3)
    idx = {s: k for k, s in enumerate(STATES)}
    trans = [[ls if a == b else lsw + PAIR_PENALTY.get((a, b), 0.0) for b in STATES] for a in STATES]
    prev = [0.0 if s == "awake" else -1.0 for s in STATES]
    back = []
    for i in range(n):
        zf = (feats["z_hr"][i], feats["z_irr"][i])
        u = i * EPOCH_DS / 600.0
        em = [_emission(s, zf, feats["motion"][i], feats["motion_s"][i], u, i / max(1, n - 1), bias)
              for s in STATES]
        cur, bk = [], []
        for b in range(4):
            best = max(range(4), key=lambda a: prev[a] + trans[a][b])
            cur.append(prev[best] + trans[best][b] + em[b])
            bk.append(best)
        back.append(bk)
        prev = cur
    k = max(range(4), key=lambda a: prev[a])
    path = [k]
    for i in range(n - 1, 0, -1):
        k = back[i][k]
        path.append(k)
    path.reverse()
    _ = idx
    return [STATES[k] for k in path]


def _share(stages, st):
    sleep = sum(1 for s in stages if s != "awake")
    return sum(1 for s in stages if s == st) / sleep if sleep else 0.0


def stage_night(n, s_ds, motion_events, beats):
    """→ (stages, feats, caps_appliqués)."""
    feats = epoch_features(n, s_ds, motion_events, beats)
    bias = {}
    stages = viterbi(feats, n, bias)
    capped = []
    for st, cap in CAPS.items():
        if _share(stages, st) <= cap:
            continue
        lo, hi = -6.0, 0.0
        for _ in range(8):
            mid = (lo + hi) / 2
            bias[st] = mid
            if _share(viterbi(feats, n, bias), st) > cap:
                hi = mid
            else:
                lo = mid
        bias[st] = lo
        stages = viterbi(feats, n, bias)
        capped.append(st)
    return stages, feats, capped


def summarize(stages, motion, epoch_s=30):
    """Agrégats de nuit à partir de l'hypnogramme.

    - endormissement : 1er epoch ouvrant 10 min consécutives de sommeil ;
    - fin : dernier epoch de sommeil ;
    - WASO : éveil entre endormissement et fin ;
    - réveils > 5 min : périodes d'éveil de ≥ 10 epochs après l'endormissement.
    """
    n = len(stages)
    onset = None
    for i in range(n):
        if i + 20 <= n and all(s != "awake" for s in stages[i:i + 20]):
            onset = i
            break
    if onset is None:
        onset = next((i for i, s in enumerate(stages) if s != "awake"), n)
    end = max((i for i, s in enumerate(stages) if s != "awake"), default=onset)
    d = {"deep": 0, "light": 0, "rem": 0, "awake": 0}
    for s in stages:
        d[s] += epoch_s
    waso = sum(epoch_s for s in stages[onset:end + 1] if s == "awake")
    awakenings, run, restless = 0, 0, 0
    for i in range(onset, end + 1):
        if stages[i] == "awake":
            run += 1
        else:
            if run >= 10:
                awakenings += 1
            run = 0
            if motion[i] > MOTION_RESTLESS:
                restless += epoch_s
    total = d["deep"] + d["light"] + d["rem"]
    return {**d, "total": total, "onset": onset, "end": end, "latency": onset * epoch_s,
            "waso": waso, "awakenings": awakenings, "restless": restless}
