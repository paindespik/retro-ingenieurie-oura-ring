"use strict";
// oura — interface du portail (JavaScript sans dépendance ni étape de build).
// Routage par l'URL (#/page/argument), rendu en chaînes HTML + graphes SVG.

// ------------------------------------------------------------------ outils

const $ = (s, root = document) => root.querySelector(s);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);

async function api(path, opt) {
  const r = await fetch("/api/" + path, Object.assign({ credentials: "same-origin" }, opt));
  if (r.status === 401 || r.redirected) throw new Error("session expirée — rechargez la page");
  let j = null;
  try { j = await r.json(); } catch (e) {
    throw new Error(r.ok ? "réponse illisible (session expirée ?)" : "HTTP " + r.status);
  }
  if (!r.ok) throw new Error((j && j.detail) || "HTTP " + r.status);
  return j;
}
const post = (p, body) => api(p, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });

const NF = {};
function num(v, d = 0) {
  if (v == null || Number.isNaN(+v)) return "—";
  NF[d] ??= new Intl.NumberFormat("fr-FR", { minimumFractionDigits: d, maximumFractionDigits: d });
  return NF[d].format(v);
}
function signed(v, d = 0) {
  if (v == null) return "—";
  const r = +(+v).toFixed(d);
  return (r > 0 ? "+" : r < 0 ? "−" : "±") + num(Math.abs(r), d);
}
function dur(h) {
  if (h == null) return "—";
  const m = Math.round(h * 60);
  return m >= 60 ? `${Math.floor(m / 60)} h ${String(m % 60).padStart(2, "0")}` : `${m} min`;
}
function hm(t) {
  return t == null ? "—" : new Date(t * 1000).toLocaleTimeString("fr-FR", { hour: "2-digit", minute: "2-digit" });
}
function hmDec(h) {
  if (h == null) return "—";
  const m = Math.round((((h % 24) + 24) % 24) * 60);
  return String(Math.floor(m / 60) % 24).padStart(2, "0") + ":" + String(m % 60).padStart(2, "0");
}
function dayLabel(d, opt = { weekday: "short", day: "numeric", month: "short" }) {
  return d ? new Date(d + "T12:00:00").toLocaleDateString("fr-FR", opt) : "—";
}
const dayLong = (d) => dayLabel(d, { weekday: "long", day: "numeric", month: "long" });
function dtShort(t) {
  return t == null ? "—" : new Date(t * 1000).toLocaleString("fr-FR",
    { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" });
}
function ago(t) {
  if (t == null) return "—";
  const s = Date.now() / 1000 - t;
  if (s < 90) return "à l'instant";
  if (s < 3600) return `il y a ${Math.round(s / 60)} min`;
  if (s < 86400) return `il y a ${Math.floor(s / 3600)} h ${String(Math.floor((s % 3600) / 60)).padStart(2, "0")}`;
  return `il y a ${Math.floor(s / 86400)} j`;
}
function isoDay(d) {
  const z = new Date(d);
  z.setMinutes(z.getMinutes() - z.getTimezoneOffset());
  return z.toISOString().slice(0, 10);
}
const today = () => isoDay(new Date());
function shiftDay(d, k) { const x = new Date(d + "T12:00:00"); x.setDate(x.getDate() + k); return isoDay(x); }
const dayNoon = (d) => new Date(d + "T12:00:00").getTime() / 1000;

const RATING = { optimal: "Optimal", bon: "Bon", moyen: "Moyen", attention: "À surveiller" };
const ratingOf = (s) => s == null ? null : s >= 85 ? "optimal" : s >= 70 ? "bon" : s >= 60 ? "moyen" : "attention";
const pl = (n, w) => `${n} ${w}${Math.abs(n) > 1 ? "s" : ""}`;

// ------------------------------------------------------------------ composants

function ring(score, label, sub, href) {
  const r = ratingOf(score), C = 2 * Math.PI * 42;
  const frac = score == null ? 0 : Math.max(0, Math.min(1, score / 100));
  const tag = href ? "a" : "div";
  return `<${tag} class="ring r-${r || "none"}"${href ? ` href="${href}"` : ""}>
    <svg viewBox="0 0 100 100" aria-hidden="true">
      <circle cx="50" cy="50" r="42" fill="none" stroke="var(--panel2)" stroke-width="9"/>
      <circle cx="50" cy="50" r="42" fill="none" stroke="var(--c)" stroke-width="9" stroke-linecap="round"
        stroke-dasharray="${(C * frac).toFixed(1)} ${C.toFixed(1)}" transform="rotate(-90 50 50)"/>
      <text class="v" x="50" y="60" text-anchor="middle">${score == null ? "—" : Math.round(score)}</text>
    </svg><div class="lbl">${esc(label)}</div><div class="sub">${sub ?? (r ? RATING[r] : "")}</div></${tag}>`;
}

function contribRow(name, c, value) {
  if (!c) return "";
  const r = c.rating || ratingOf(c.score);
  return `<div class="contrib r-${r || "none"}"><span class="name">${esc(name)}</span>
    <span class="rate">${c.score == null ? "—" : `${RATING[r]} · ${Math.round(c.score)}`}</span>
    <div class="bar"><i style="width:${Math.max(2, Math.min(100, c.score || 0))}%"></i></div>
    ${value ? `<span class="val">${value}</span>` : ""}</div>`;
}

function tile(k, v, unit, d, cls) {
  return `<div class="tile"><div class="k">${k}</div><div class="v">${v}${unit ? `<small>${unit}</small>` : ""}</div>
    ${d ? `<div class="d ${cls || ""}">${d}</div>` : ""}</div>`;
}

// écart à la ligne de base : { html, cls }. bad = sens défavorable (+1 hausse, −1 baisse)
function vsBase(v, b, { d = 0, unit = "", bad = 0, thr = 0, rel = false } = {}) {
  if (v == null) return { html: "", cls: "" };
  if (!b || b.n < 3) return { html: `référence en calibrage (${b ? b.n : 0}/3 nuits)`, cls: "" };
  const delta = v - b.mean;
  const limit = rel ? thr * b.mean : thr;
  const cls = bad && Math.sign(delta) === bad && Math.abs(delta) >= limit ? "up-bad" : "";
  return { html: `${signed(delta, d)}${unit ? " " + unit : ""} vs réf. ${num(b.mean, d)}${b.n < 14 ? " (provisoire)" : ""}`, cls };
}

function vitalTiles(n, B) {
  if (!n) return `<div class="muted">aucune nuit</div>`;
  const out = [];
  let x = vsBase(n.hr_min, B.hr_min, { d: 1, unit: "bpm", bad: 1, thr: 5 });
  out.push(tile("FC la plus basse", num(n.hr_min, 0), "bpm",
    `${n.hr_lowest_at ? "à " + hm(n.hr_lowest_at) + " · " : ""}${x.html}`, x.cls));
  x = vsBase(n.hrv_rmssd, B.hrv_rmssd, { d: 0, unit: "ms", bad: -1, thr: 0.25, rel: true });
  out.push(tile("HRV moyen", num(n.hrv_rmssd, 0), "ms", x.html, x.cls));
  const st = n.temp_status;
  out.push(tile("Température (écart)", n.temp_dev == null ? "—" : signed(n.temp_dev, 1), n.temp_dev == null ? "" : "°C",
    st === "fiable" ? `nuit ${num(n.temp_mean, 2)} °C` :
      `${st === "provisoire" ? "référence provisoire" : "calibrage"} (${n.temp_n || 0} nuits)`,
    n.temp_dev != null && Math.abs(n.temp_dev) >= 0.5 ? "up-bad" : ""));
  x = vsBase(n.resp_rate, B.resp_rate, { d: 1, unit: "/min", bad: 1, thr: 2 });
  out.push(tile("Respiration <span class='badge'>expérimental</span>", num(n.resp_rate, 1), "/min", x.html, x.cls));
  out.push(tile("Indice de récupération", dur(n.recovery_index), "",
    "sommeil après la FC la plus basse (optimal ≥ 6 h)", n.recovery_index != null && n.recovery_index < 4 ? "up-bad" : ""));
  x = vsBase(n.hr_mean, B.hr_mean, { d: 1, unit: "bpm", bad: 1, thr: 8 });
  out.push(tile("FC moyenne (sommeil)", num(n.hr_mean, 0), "bpm", x.html, x.cls));
  return out.join("");
}

function tensionCard(t, day) {
  if (!t || !t.level) return "";
  if (t.level === "aucun") {
    return `<div class="card"><h2>Signes de tension <span class="badge ok">aucun</span></h2>
      <div class="small muted">Aucun écart notable par rapport à vos nuits précédentes${t.n_base < 3 ? " (pas encore assez de nuits de référence)" : ""}.</div></div>`;
  }
  const items = t.signals.map((s) => s.metric === "temp_dev"
    ? `<li><b>${esc(s.label)}</b> : ${signed(s.value, 2)} °C par rapport à votre référence${s.strong ? " (fort)" : ""}</li>`
    : `<li><b>${esc(s.label)}</b> : ${num(s.value, 1)} ${s.unit} (${signed(s.delta, 1)} vs réf. ${num(s.baseline, 1)})${s.strong ? " (fort)" : ""}</li>`).join("");
  return `<div class="card alert lv-${t.level}"><h2>Signes de tension ${t.level}
      ${t.provisional ? `<span class="badge warn">référence provisoire · ${pl(t.n_base, "nuit")}</span>` : ""}
      ${day ? `<span class="right muted small">${dayLong(day)}</span>` : ""}</h2>
    <ul>${items}</ul>
    <div class="small">Des écarts simultanés de ce type s'observent par exemple lors d'une infection ou d'une fièvre,
    après de l'alcool ou un repas tardif, ou en période de stress ou de manque de sommeil. <b>Ce n'est pas un diagnostic.</b>
    Reposez-vous, notez le contexte dans le <a href="#/journal">journal</a> ; en cas de malaise, mesurez votre température
    avec un thermomètre. Consultez un médecin si cela persiste plusieurs nuits ou si des symptômes inquiétants
    apparaissent (douleur thoracique, essoufflement : 15 ou 112).</div></div>`;
}

function dayNav(base, day, prev, next, fmt = dayLong) {
  return `<div class="daynav">
    <button class="ghost" ${prev ? `onclick="location.hash='${base}/${prev}'"` : "disabled"} aria-label="précédent">‹</button>
    <span class="d">${fmt(day)}</span>
    <button class="ghost" ${next ? `onclick="location.hash='${base}/${next}'"` : "disabled"} aria-label="suivant">›</button></div>`;
}

// ------------------------------------------------------------------ graphes

const tipEl = () => $("#tip");
function showTip(ev, html) {
  const t = tipEl();
  t.innerHTML = html;
  t.hidden = false;
  const w = t.offsetWidth, h = t.offsetHeight;
  let x = ev.clientX + 14, y = ev.clientY - h - 10;
  if (x + w > innerWidth - 6) x = ev.clientX - w - 14;
  if (y < 6) y = ev.clientY + 16;
  t.style.left = x + "px";
  t.style.top = y + "px";
}
const hideTip = () => { tipEl().hidden = true; };

function chartWidth() {
  return Math.max(300, Math.min(1040, ($("#main").clientWidth || 800) - 34));
}
const TSTEPS = [300, 600, 900, 1800, 3600, 7200, 10800, 21600, 43200, 86400, 172800, 604800, 1209600];
function timeTicks(x0, x1, n) {
  const step = TSTEPS.find((s) => (x1 - x0) / s <= n) || 2592000;
  const off = new Date(x0 * 1000).getTimezoneOffset() * 60;
  const out = [];
  for (let t = Math.ceil((x0 - off) / step) * step + off; t <= x1; t += step) out.push(t);
  return { ticks: out, step };
}
function dayTicks(x0, x1, n) {
  // graduations au midi local de chaque jour (les points « jour » y sont placés)
  const days = Math.max(1, Math.round((x1 - x0) / 86400));
  const k = [1, 2, 3, 7, 14, 30].find((s) => days / s <= n) || 30;
  const d = new Date(x0 * 1000);
  d.setHours(12, 0, 0, 0);
  if (d.getTime() / 1000 < x0) d.setDate(d.getDate() + 1);
  const out = [];
  for (; d.getTime() / 1000 <= x1; d.setDate(d.getDate() + k)) out.push(d.getTime() / 1000);
  return { ticks: out, step: 86400 * k };
}
function tickLabel(t, step) {
  const d = new Date(t * 1000);
  if (step >= 86400 || (d.getHours() === 0 && d.getMinutes() === 0))
    return d.toLocaleDateString("fr-FR", { day: "numeric", month: "short" });
  return hm(t);
}
function niceTicks(lo, hi, n = 4) {
  const span = hi - lo || 1, raw = span / n, mag = 10 ** Math.floor(Math.log10(raw));
  const step = [1, 2, 2.5, 5, 10].map((k) => k * mag).find((s) => span / s <= n) || 10 * mag;
  const out = [];
  for (let v = Math.ceil(lo / step) * step; v <= hi + step * 1e-6; v += step) out.push(+v.toFixed(8));
  return out;
}
function nearest(points, t) {
  let lo = 0, hi = points.length - 1;
  if (hi < 0) return null;
  while (hi - lo > 1) { const mid = (lo + hi) >> 1; if (points[mid][0] < t) lo = mid; else hi = mid; }
  return Math.abs(points[lo][0] - t) <= Math.abs(points[hi][0] - t) ? points[lo] : points[hi];
}

/**
 * Graphe temporel. o = {series:[{name, color, unit, dec, type:"line"|"bar"|"dots", points:[[t,v]],
 * colorOf?, binW?, fmt?}], x0, x1, y0, y1, height, band:[lo,hi,label], shades:[{from,to,color}],
 * markers:[{t,v,label,color}], hline:{v,label}, dayAxis:boolean, empty}
 */
function timeChart(o) {
  const box = document.createElement("div");
  box.className = "chart";
  const series = o.series.map((s) => ({ ...s, points: (s.points || []).filter((p) => p[1] != null && !Number.isNaN(p[1])).sort((a, b) => a[0] - b[0]) }));
  const all = series.flatMap((s) => s.points);
  if (!all.length) { box.innerHTML = `<div class="muted small">${o.empty || "pas de données sur la période"}</div>`; return box; }
  const W = chartWidth(), H = o.height || 170, L = 40, R = 10, T = 10, B = 22;
  let x0 = o.x0 ?? Math.min(...all.map((p) => p[0])), x1 = Math.max(o.x1 ?? Math.max(...all.map((p) => p[0])), x0 + 60);
  if (o.dayAxis) { x0 -= 43200; x1 += 43200; }        // une demi-journée de marge autour des barres
  const extra = [...(o.band ? [o.band[0], o.band[1]] : []), ...(o.hline ? [o.hline.v] : [])];
  let y0 = o.y0 ?? Math.min(...all.map((p) => p[1]), ...extra);
  let y1 = o.y1 ?? Math.max(...all.map((p) => p[1]), ...extra);
  if (y1 - y0 < (o.minSpan || 1e-6)) { const m = (y0 + y1) / 2, h = (o.minSpan || 1) / 2; y0 = m - h; y1 = m + h; }
  const pad = (y1 - y0) * 0.12;
  if (o.y0 == null) y0 -= pad;
  if (o.y1 == null) y1 += pad;
  const X = (t) => L + ((t - x0) / (x1 - x0)) * (W - L - R);
  const Y = (v) => T + (1 - (v - y0) / (y1 - y0)) * (H - T - B);
  let g = `<svg viewBox="0 0 ${W} ${H}" width="${W}" height="${H}" role="img">`;
  for (const s of o.shades || []) {
    const a = Math.max(x0, s.from), b = Math.min(x1, s.to);
    if (b > a) g += `<rect x="${X(a)}" y="${T}" width="${Math.max(1, X(b) - X(a))}" height="${H - T - B}" fill="${s.color}"/>`;
  }
  if (o.band) {
    g += `<rect x="${L}" y="${Y(o.band[1])}" width="${W - L - R}" height="${Math.max(1, Y(o.band[0]) - Y(o.band[1]))}"
      fill="var(--accent)" opacity=".10"/>`;
  }
  g += `<g class="ax">`;
  for (const v of niceTicks(y0, y1, o.yTicks || 4)) {
    g += `<line class="grid-l" x1="${L}" x2="${W - R}" y1="${Y(v)}" y2="${Y(v)}"/>
      <text x="${L - 6}" y="${Y(v) + 4}" text-anchor="end">${num(v, o.yDec ?? (Math.abs(y1 - y0) < 5 ? 1 : 0))}</text>`;
  }
  const tt = o.dayAxis ? dayTicks(x0, x1, Math.max(3, Math.floor(W / 90))) : timeTicks(x0, x1, Math.max(3, Math.floor(W / 110)));
  for (const t of tt.ticks) {
    g += `<line class="grid-l" x1="${X(t)}" x2="${X(t)}" y1="${T}" y2="${H - B}" opacity=".5"/>
      <text x="${X(t)}" y="${H - 6}" text-anchor="middle">${o.dayAxis ? dayLabel(isoDay(t * 1000), { day: "numeric", month: "short" }) : tickLabel(t, tt.step)}</text>`;
  }
  g += `</g>`;
  if (o.hline) {
    g += `<line x1="${L}" x2="${W - R}" y1="${Y(o.hline.v)}" y2="${Y(o.hline.v)}" stroke="var(--muted)" stroke-dasharray="4 4"/>`;
  }
  for (const s of series) {
    const P = s.points;
    if (s.type === "bar") {
      const bw = s.binW || (P.length > 1 ? Math.min(...P.slice(1).map((p, i) => p[0] - P[i][0]).filter((d) => d > 0)) : 60);
      const w = Math.max(1, X(x0 + bw) - X(x0) - (bw >= 3600 ? 3 : 0.4));
      const base = Y(Math.max(y0, Math.min(y1, s.base ?? 0)));
      for (const p of P) {
        const y = Y(p[1]);
        g += `<rect x="${X(p[0]) - (s.center ? w / 2 : 0)}" y="${Math.min(y, base)}" width="${w}" height="${Math.max(0.8, Math.abs(base - y))}"
          fill="${s.colorOf ? s.colorOf(p[1]) : s.color}" rx="${w > 4 ? 1.5 : 0}"/>`;
      }
    } else if (s.type === "dots") {
      for (const p of P) g += `<circle cx="${X(p[0])}" cy="${Y(p[1])}" r="${s.r || 1.8}" fill="${s.color}" opacity="${s.opacity || 0.8}"/>`;
    } else {
      const dts = P.slice(1).map((p, i) => p[0] - P[i][0]).sort((a, b) => a - b);
      const gap = s.gap || Math.max(4 * (dts[dts.length >> 1] || 60), 600);
      let seg = [];
      const flush = () => {
        if (seg.length > 1) g += `<polyline fill="none" stroke="${s.color}" stroke-width="${s.width || 2}" stroke-linejoin="round"
          ${s.dash ? `stroke-dasharray="${s.dash}"` : ""} points="${seg.map((p) => X(p[0]).toFixed(1) + "," + Y(p[1]).toFixed(1)).join(" ")}"/>`;
        else if (seg.length === 1) g += `<circle cx="${X(seg[0][0])}" cy="${Y(seg[0][1])}" r="2" fill="${s.color}"/>`;
        seg = [];
      };
      P.forEach((p, i) => { if (i && p[0] - P[i - 1][0] > gap) flush(); seg.push(p); });
      flush();
    }
  }
  for (const mk of o.markers || []) {
    g += `<circle cx="${X(mk.t)}" cy="${Y(mk.v)}" r="5" fill="none" stroke="${mk.color || "var(--fg)"}" stroke-width="2.2"/>`;
  }
  g += `<line class="cursor" x1="0" x2="0" y1="${T}" y2="${H - B}" visibility="hidden"/></svg>`;
  box.innerHTML = g;
  const mkLeg = (o.markers || []).filter((mk) => mk.label);
  if ((o.legend !== false && series.length > 1) || o.legend === true || mkLeg.length) {
    box.insertAdjacentHTML("beforeend", `<div class="legend">${series.filter((s) => s.name).map((s) =>
      `<span style="--c:${s.color}"><i></i>${esc(s.name)}</span>`).join("")}${mkLeg.map((mk) =>
      `<span>◯ ${esc(mk.label)}</span>`).join("")}${o.band && o.band[2] ? `<span style="--c:var(--accent)"><i style="opacity:.35;height:.6rem"></i>${esc(o.band[2])}</span>` : ""}</div>`);
  }
  const svg = box.querySelector("svg"), cur = box.querySelector(".cursor");
  const move = (ev) => {
    const rect = svg.getBoundingClientRect();
    const px = ((ev.clientX - rect.left) / rect.width) * W;
    if (px < L || px > W - R) { hideTip(); cur.setAttribute("visibility", "hidden"); return; }
    const t = x0 + ((px - L) / (W - L - R)) * (x1 - x0);
    const rows = [];
    let tRef = null;
    for (const s of series) {
      const p = nearest(s.points, t);
      if (!p || Math.abs(p[0] - t) > (s.tipRange || (x1 - x0) / 40)) continue;
      tRef ??= p[0];
      rows.push(`<span style="color:${s.color}">●</span> ${esc(s.name)} : <b>${s.fmt ? s.fmt(p[1]) : num(p[1], s.dec ?? 0)}</b>${s.unit ? " " + s.unit : ""}`);
    }
    if (!rows.length) { hideTip(); cur.setAttribute("visibility", "hidden"); return; }
    cur.setAttribute("x1", X(tRef)); cur.setAttribute("x2", X(tRef)); cur.setAttribute("visibility", "visible");
    showTip(ev, `<div class="muted">${o.dayAxis ? dayLong(isoDay(tRef * 1000)) : dtShort(tRef)}</div>${rows.join("<br>")}`);
  };
  svg.addEventListener("pointermove", move);
  svg.addEventListener("pointerdown", move);
  svg.addEventListener("pointerleave", () => { hideTip(); cur.setAttribute("visibility", "hidden"); });
  return box;
}

const STAGE = { W: { cls: "awake", name: "Éveil", row: 0 }, R: { cls: "rem", name: "REM", row: 1 },
  L: { cls: "light", name: "Léger", row: 2 }, D: { cls: "deep", name: "Profond", row: 3 } };

function hypnogram(stg, o = {}) {
  const box = document.createElement("div");
  box.className = "chart hyp";
  if (!stg || !stg.stages || !stg.start) { box.innerHTML = `<div class="muted small">pas d'hypnogramme</div>`; return box; }
  const n = stg.stages.length, ep = stg.epoch_s || 30, x0 = stg.start, x1 = stg.start + n * ep;
  const W = chartWidth(), H = o.height || 170, L = o.compact ? 8 : 58, R = 10, T = 6, B = 20;
  const rowH = (H - T - B) / 4;
  const X = (t) => L + ((t - x0) / (x1 - x0)) * (W - L - R);
  const runs = [];
  for (let i = 0; i < n; i++) {
    const c = stg.stages[i];
    if (runs.length && runs[runs.length - 1].c === c) runs[runs.length - 1].n++;
    else runs.push({ c, i, n: 1 });
  }
  let g = `<svg viewBox="0 0 ${W} ${H}" width="${W}" height="${H}" role="img" aria-label="hypnogramme">`;
  if (!o.compact) {
    for (const k of "WRLD") {
      const s = STAGE[k];
      g += `<text x="${L - 8}" y="${T + s.row * rowH + rowH / 2 + 4}" text-anchor="end">${s.name}</text>`;
    }
  }
  const tt = timeTicks(x0, x1, Math.max(3, Math.floor(W / 100)));
  for (const t of tt.ticks) {
    g += `<line x1="${X(t)}" x2="${X(t)}" y1="${T}" y2="${H - B}" stroke="var(--line)" opacity=".6"/>
      <text x="${X(t)}" y="${H - 5}" text-anchor="middle">${hm(t)}</text>`;
  }
  let prev = null;
  for (const r of runs) {
    const s = STAGE[r.c] || STAGE.L;
    const a = X(x0 + r.i * ep), b = X(x0 + (r.i + r.n) * ep);
    const y = T + s.row * rowH;
    if (prev) {
      const py = T + prev.row * rowH;
      g += `<line x1="${a}" x2="${a}" y1="${Math.min(py, y) + rowH / 2}" y2="${Math.max(py, y) + rowH / 2}" stroke="var(--line)" stroke-width="1"/>`;
    }
    g += `<rect x="${a}" y="${y + rowH * 0.06}" width="${Math.max(1, b - a)}" height="${rowH * 0.88}" rx="2" fill="var(--st-${s.cls})"/>`;
    prev = s;
  }
  g += `</svg>`;
  box.innerHTML = g;
  const svg = box.querySelector("svg");
  const move = (ev) => {
    const rect = svg.getBoundingClientRect();
    const px = ((ev.clientX - rect.left) / rect.width) * W;
    const i = Math.floor(((px - L) / (W - L - R)) * n);
    const r = runs.find((q) => i >= q.i && i < q.i + q.n);
    if (!r) return hideTip();
    const a = x0 + r.i * ep, b = x0 + (r.i + r.n) * ep;
    showTip(ev, `<b>${STAGE[r.c].name}</b><br>${hm(a)} → ${hm(b)} (${dur((b - a) / 3600)})`);
  };
  svg.addEventListener("pointermove", move);
  svg.addEventListener("pointerdown", move);
  svg.addEventListener("pointerleave", hideTip);
  return box;
}

function stageBreakdown(n) {
  const parts = [["deep", n.deep, "Profond"], ["rem", n.rem, "REM"], ["light", n.light, "Léger"], ["awake", n.awake, "Éveil"]];
  const tot = parts.reduce((a, p) => a + (p[1] || 0), 0) || 1;
  const sleep = (n.deep || 0) + (n.rem || 0) + (n.light || 0) || 1;
  return `<div class="stages">${parts.map((p) => `<i class="st-${p[0]}" style="width:${(100 * (p[1] || 0)) / tot}%;background:var(--c)"></i>`).join("")}</div>
    <div class="stage-legend">${parts.map((p) => `<span class="st-${p[0]}">${p[2]} ${dur(p[1])}${p[0] !== "awake" ? ` · ${num((100 * (p[1] || 0)) / sleep)} %` : ""}</span>`).join("")}</div>`;
}

// ------------------------------------------------------------------ libellés

const SLEEP_CONTRIB = {
  total_sleep: "Sommeil total", efficiency: "Efficacité", restfulness: "Tranquillité",
  rem: "Sommeil paradoxal (REM)", deep: "Sommeil profond", latency: "Latence d'endormissement", timing: "Horaire",
};
const READY_CONTRIB = {
  rhr: "FC de repos", hrv_balance: "Équilibre HRV", temperature: "Température corporelle",
  recovery_index: "Indice de récupération", sleep: "Sommeil de la nuit", sleep_balance: "Équilibre du sommeil",
  sleep_regularity: "Régularité du sommeil", previous_day_activity: "Activité de la veille",
  activity_balance: "Équilibre de l'activité",
};
const ACT_CONTRIB = {
  stay_active: "Rester actif", move_every_hour: "Bouger chaque heure", training_frequency: "Fréquence d'entraînement",
  training_volume: "Volume d'entraînement", recovery_time: "Jours de récupération",
};

function sleepContribValue(k, n) {
  const sleep = (n.deep || 0) + (n.rem || 0) + (n.light || 0) || 1;
  return {
    total_sleep: `${dur(n.total)} (repère adulte 7–9 h)`,
    efficiency: `${num(n.efficiency)} % du temps au lit (repère > 85 %)`,
    restfulness: `éveil après endormissement ${num(n.waso)} min · ${pl(n.awakenings ?? 0, "réveil")} > 5 min · ${num(n.movement)} min agitées`,
    rem: `${dur(n.rem)} · ${num((100 * n.rem) / sleep)} % (repère adulte 21–30 %)`,
    deep: `${dur(n.deep)} · ${num((100 * n.deep) / sleep)} % (repère adulte 16–20 %)`,
    latency: `${num(n.latency)} min (repère < 30 min)`,
    timing: `milieu du sommeil ${hmDec(n.timing)} (repère 00:00–03:00 selon le chronotype)`,
  }[k];
}

function readyContribValue(k, c) {
  switch (k) {
    case "rhr": return `${num(c.value, 1)} bpm · ${signed(c.delta, 1)} vs réf. ${num(c.baseline, 1)}`;
    case "hrv_balance": return `${num(c.value, 0)} ms (14 j pondérés) vs ${num(c.baseline, 0)} ms · ${signed(c.delta, 0)} %`;
    case "temperature": return `${signed(c.value, 2)} °C · référence ${c.status}`;
    case "recovery_index": return `${dur(c.value)} de sommeil après la FC la plus basse`;
    case "sleep": return dur(c.value);
    case "sleep_balance": return `moyenne pondérée ${dur(c.value)} / besoin ${num(c.need)} h`;
    case "sleep_regularity": return `écart-type du milieu de sommeil ${c.value} min`;
    case "previous_day_activity": return `${num(c.value)} MET-min · ${dur(c.inactive_min / 60)} inactif`;
    default: return "";
  }
}

function actContribValue(k, c) {
  switch (k) {
    case "stay_active": return `${dur(c.value / 60)} inactif (bien : ≤ 8 h ; attention > 12 h)`;
    case "move_every_hour": return `${pl(c.value, "période")} sédentaire${c.value > 1 ? "s" : ""} > 50 min`;
    case "training_frequency": return `${pl(c.value, "jour")} ≥ 100 MET-min sur ${c.days} j`;
    case "training_volume": return `${num(c.value)} MET-min / semaine (cible 2 000, sur ${c.days} j)`;
    case "recovery_time": return `${pl(c.value, "journée")} légère${c.value > 1 ? "s" : ""} sur ${c.days} j`;
    default: return "";
  }
}

// ------------------------------------------------------------------ pages

async function pageToday(m) {
  const o = await api("overview");
  setFresh(o.ring);
  const n = o.night, rd = o.readiness, a = o.activity, B = o.baselines || {};
  let html = "";
  if (rd && rd.tension && rd.tension.level !== "aucun") html += tensionCard(rd.tension, n && n.night);
  const aSub = a ? (a.partial ? "journée en cours" : undefined) : "pas de données";
  const rSub = rd ? (rd.score == null ? "calibrage" : `${RATING[ratingOf(rd.score)]}${rd.provisional ? " · provisoire" : ""}`) : "—";
  html += `<div class="card"><div class="rings">
      ${ring(n ? n.score : null, "Sommeil", n ? dayLabel(n.night) : "aucune nuit", n ? "#/sommeil/" + n.night : null)}
      ${ring(rd ? rd.score : null, "Récupération", rSub, n ? "#/recuperation/" + n.night : null)}
      ${ring(a ? a.score : null, "Activité", aSub, "#/activite/" + today())}</div></div>`;
  html += `<div class="card"><h2>Constantes de la nuit <span class="right small">${n ? dayLong(n.night) : ""}</span></h2>
      <div class="tiles">${vitalTiles(n, B)}</div></div>`;
  const b = o.briefing;
  html += `<div class="grid">
    <div class="card"><h2>Nuit dernière ${n ? `<a class="right small" href="#/sommeil/${n.night}">détail ›</a>` : ""}</h2>
      <div id="mini-hyp"></div>
      ${n ? `<div class="small muted">${n.bedtime} → ${n.wake_time} · ${dur(n.total)} de sommeil · efficacité ${num(n.efficiency)} %</div>` : ""}</div>
    <div class="card"><h2>Résumé du matin ${b ? `<span class="badge">${esc(b.model)}</span>` : ""}
      ${b && b.stale ? `<span class="badge warn">données modifiées depuis</span>` : ""}</h2>
      ${b ? `<div class="brief">${esc(b.text)}</div><div class="tiny muted" style="margin-top:.4rem">nuit du ${dayLabel(b.night)} · rédigé ${ago(b.ts)} par un modèle local${b.stale ? " — régénéré automatiquement au prochain passage" : ""}</div>`
      : `<div class="muted small">Aucun résumé pour l'instant (rédigé chaque matin par le LLM local).</div>`}</div></div>`;
  const r = o.ring, live = o.live || {};
  const push = r.push && r.push.phone;
  html += `<div class="grid">
    <div class="card"><h2>Anneau</h2>
      <div class="kv"><span>Batterie</span><b>${r.battery ? `${r.battery.pct} %` : "—"}</b></div>
      <div class="kv"><span>État</span><span>${r.on_charger ? "sur le chargeur" : r.worn ? "au doigt" : r.worn === false ? "retiré" : "—"}${r.state ? ` · ${esc(r.state.label)}` : ""}</span></div>
      <div class="kv"><span>Dernier envoi du téléphone</span><span>${push ? ago(push.last_push_unix) : "—"}</span></div>
      <div class="kv"><span>Horloge</span><span>${r.axis.mode === "sync" ? "synchronisée" : "estimée"}${r.axis.drift_s ? ` (recalée de ${num(r.axis.drift_s / 60)} min)` : ""}</span></div></div>
    <div class="card"><h2>En ce moment</h2>
      <div class="kv"><span>Température cutanée</span><span>${r.skin_temp && r.skin_temp.v != null ? `<b>${num(r.skin_temp.v, 1)} °C</b> · ${ago(r.skin_temp.t)}` : "—"}</span></div>
      <div class="kv"><span>Fréquence cardiaque</span><span>${live.hr ? `<b>${live.hr.v} bpm</b> · ${ago(live.hr.t)}` : "—"}</span></div>
      <div class="kv"><span>Intensité (MET)</span><span>${live.met ? `<b>${num(live.met.v, 1)}</b> · ${ago(live.met.t)}` : "—"}</span></div>
      <div class="kv"><span>Activité du jour</span><span>${a ? `${num(a.met_min_mh)} MET-min · ${num(a.active_kcal)} kcal actives (est.)` : "—"}</span></div></div></div>`;
  m.innerHTML = html;
  if (o.staging && $("#mini-hyp", m)) $("#mini-hyp", m).append(hypnogram(o.staging, { height: 100, compact: true }));
}

async function pageSleep(m, date) {
  if (!date) {
    const l = await api("nights");
    if (!l.nights.length) { m.innerHTML = `<div class="card muted">Aucune nuit dérivée pour l'instant.</div>`; return; }
    date = l.nights[l.nights.length - 1].night;
  }
  const d = await api("night?date=" + date);
  const n = d.night, B = d.baselines || {}, sub = n.subscores || {};
  const S = d.series || [];
  let html = `<div class="card"><div class="row" style="justify-content:space-between">
      ${dayNav("#/sommeil", date, d.prev, d.next)}<span class="badge">${esc(n.staging_source)}</span></div>
    <div class="row" style="margin-top:.6rem;align-items:flex-start">
      <div style="flex:0 0 auto">${ring(n.score, "Score de sommeil", d.prev_row && n.score != null && d.prev_row.score != null ? `${signed(n.score - d.prev_row.score)} vs nuit préc.` : undefined)}</div>
      <div class="tiles" style="flex:1 1 320px">
        ${tile("Sommeil total", dur(n.total), "", `au lit ${dur(n.in_bed)}`)}
        ${tile("Efficacité", num(n.efficiency), "%", "repère > 85 %")}
        ${tile("Latence", num(n.latency), "min", "repère < 30 min", n.latency > 30 ? "up-bad" : "")}
        ${tile("Éveil après endormissement", num(n.waso), "min", `${pl(n.awakenings ?? 0, "réveil")} > 5 min`, n.waso > 20 ? "up-bad" : "")}
        ${tile("Au lit", `${n.bedtime}–${n.wake_time}`, "", `endormi ${hm(n.onset_unix)} · réveil ${hm(n.end_unix)}`)}
        ${tile("Milieu du sommeil", hmDec(n.timing), "", "repère 00:00–03:00")}
      </div></div></div>`;
  html += `<div class="card"><h2>Hypnogramme <span class="right small muted">estimation — voir « Méthode »</span></h2>
    <div id="hyp"></div>${stageBreakdown(n)}
    <div class="tiny muted" style="margin-top:.3rem">Repères adultes 26–64 ans (National Sleep Foundation 2017) : profond 16–20 %, REM 21–30 % du sommeil.</div></div>`;
  html += `<div class="grid"><div class="card"><h2>Contributeurs du score</h2>
      ${Object.keys(SLEEP_CONTRIB).map((k) => contribRow(SLEEP_CONTRIB[k], sub[k] == null ? null : { score: sub[k] }, sleepContribValue(k, n))).join("")}</div>
    <div class="card"><h2>Constantes</h2><div class="tiles">${vitalTiles(n, B)}</div></div></div>`;
  html += `<div class="card"><h2>Fréquence cardiaque <span class="right small muted">bins de 5 min calculés par l'anneau</span></h2><div id="c-hr"></div></div>
    <div class="grid"><div class="card"><h2>HRV (RMSSD)</h2><div id="c-hrv"></div></div>
    <div class="card"><h2>Respiration <span class="badge">expérimental</span></h2><div id="c-resp"></div></div></div>
    <div class="grid"><div class="card"><h2>Mouvement</h2><div id="c-mot"></div></div>
    <div class="card"><h2>Température cutanée</h2><div id="c-temp"></div></div></div>`;
  if (d.readiness) {
    html += `<div class="card"><h2>Récupération du jour <a class="right small" href="#/recuperation/${date}">détail ›</a></h2>
      <div class="row">${ring(d.readiness.score, "Récupération", d.readiness.score == null ? "calibrage" : undefined)}
      <div class="small muted" style="flex:1 1 240px">${d.readiness.tension && d.readiness.tension.level !== "aucun"
        ? `<b style="color:var(--bad)">Signes de tension ${d.readiness.tension.level}</b> : ${d.readiness.tension.signals.map((s) => esc(s.label)).join(", ")}.`
        : "Aucun signe de tension notable."}</div></div></div>`;
  }
  html += journalCard(shiftDay(date, -1), d.tags || []);
  if (d.briefing) {
    html += `<div class="card"><h2>Résumé du matin <span class="badge">${esc(d.briefing.model)}</span>
      ${d.briefing.ts < (n.ts || 0) ? `<span class="badge warn">rédigé avant le dernier calcul — régénération au prochain passage</span>` : ""}</h2>
      <div class="brief">${esc(d.briefing.text)}</div><div class="tiny muted">rédigé ${dtShort(d.briefing.ts)}</div></div>`;
  }
  html += `<div class="card"><details><summary>Méthode et limites</summary><div class="small">
    <p><b>Fenêtre de sommeil</b> : détectée par l'anneau (analyse embarquée « bedtime »). <b>Stades</b> : heuristique ouverte
    (mouvement de l'accéléromètre + FC et régularité de la FC battement par battement, lissage par modèle de Markov caché,
    a priori circadiens : peu de REM dans la première heure, profond plutôt en début de nuit), avec des plafonds
    physiologiques (profond ≤ 25 %, REM ≤ 30 %). Non validée contre polysomnographie : l'éveil calme et le REM restent
    difficiles à distinguer, les durées par stade sont indicatives.</p>
    <p><b>FC la plus basse</b> : minimum des moyennes sur 10 min pendant le sommeil. <b>HRV</b> : moyenne des RMSSD
    sur 5 min calculés par l'anneau. <b>Indice de récupération</b> : sommeil restant après la FC la plus basse
    (Oura : optimal ≥ 6 h). <b>Respiration</b> : estimée à partir de l'arythmie sinusale respiratoire (variation de la FC
    avec la respiration) — expérimentale, non vérifiée contre une référence. <b>Température</b> : capteur cutané pendant le
    sommeil ; l'écart est comparé à la médiane de vos nuits précédentes (provisoire avant 14 nuits).</p>
    <p><b>Score</b> : 7 contributeurs pondérés 35/15/10/10/10/10/10 (pondération Oura publiée), barèmes publics pour
    durée/stades/efficacité/latence/horaire et barème local pour la tranquillité (seuils NSF). ${esc(n.score_source || "")}</p>
    <p class="muted">Axe horaire : ${esc(n.axis_mode)} · source FC : ${esc(n.hr_source)} · HRV recalculé sur les battements
    (contrôle) : ${num(n.hrv_beats)} ms · calcul ${dtShort(n.ts)}</p></div></details></div>`;
  m.innerHTML = html;
  $("#hyp", m).append(hypnogram(d.staging, { height: 180 }));
  const x0 = n.start_unix, x1 = n.stop_unix;
  const pts = (k) => S.map((p) => [p.t, p[k]]);
  const hrB = B.hr_min;
  $("#c-hr", m).append(timeChart({ x0, x1, height: 170, series: [{ name: "FC", color: "var(--hr)", unit: "bpm", points: pts("hr") }],
    markers: n.hr_lowest_at ? [{ t: n.hr_lowest_at, v: n.hr_min, label: `FC la plus basse ${num(n.hr_min)} bpm à ${hm(n.hr_lowest_at)}`, color: "var(--hr)" }] : [],
    band: hrB && hrB.n >= 3 ? [hrB.mean - Math.max(hrB.sd, 1), hrB.mean + Math.max(hrB.sd, 1), "FC la plus basse habituelle (± 1 écart-type)"] : null, legend: true }));
  $("#c-hrv", m).append(timeChart({ x0, x1, height: 140, series: [{ name: "RMSSD", color: "var(--hrv)", unit: "ms", points: pts("rmssd") }],
    hline: n.hrv_rmssd ? { v: n.hrv_rmssd } : null }));
  $("#c-resp", m).append(timeChart({ x0, x1, height: 140, series: [{ name: "Respiration", color: "var(--resp)", unit: "/min", dec: 1, points: pts("resp") }],
    minSpan: 4, empty: "pas assez de battements propres pour estimer la respiration" }));
  $("#c-mot", m).append(timeChart({ x0, x1, height: 120, series: [{ name: "Mouvement (max 5 min)", color: "var(--motion)", type: "bar", binW: 300, dec: 2,
    points: S.map((p) => [p.t - 150, p.motion == null ? null : Math.min(p.motion, 10)]) }], y0: 0 }));
  $("#c-temp", m).append(timeChart({ x0, x1, height: 140, series: [{ name: "Température", color: "var(--temp)", unit: "°C", dec: 2, points: pts("temp") }], minSpan: 0.5 }));
  bindJournal(m);
}

async function pageRecovery(m, date) {
  const list = await api("readiness?days=90");
  const days = list.days || [];
  if (!days.length) { m.innerHTML = `<div class="card muted">Pas encore de données de récupération.</div>`; return; }
  const i = date ? days.findIndex((x) => x.day === date) : days.length - 1;
  const r = days[i < 0 ? days.length - 1 : i];
  const idx = days.indexOf(r);
  const c = r.contributors || {};
  let html = `<div class="card"><div class="row" style="justify-content:space-between">
      ${dayNav("#/recuperation", r.day, idx > 0 ? days[idx - 1].day : null, idx < days.length - 1 ? days[idx + 1].day : null)}
      ${r.provisional ? `<span class="badge warn">référence provisoire · ${pl(r.n_base, "nuit")} (fiable à partir de 14)</span>` : ""}</div>
    <div class="row" style="margin-top:.6rem;align-items:flex-start">
      <div>${ring(r.score, "Récupération", r.score == null ? `calibrage (${Object.keys(c).length}/5 contributeurs)` : undefined)}</div>
      <div style="flex:1 1 320px">${Object.keys(READY_CONTRIB).map((k) => c[k] ? contribRow(READY_CONTRIB[k], c[k], readyContribValue(k, c[k])) : "").join("")}
      ${Object.keys(READY_CONTRIB).filter((k) => !c[k]).length ? `<div class="tiny muted" style="margin-top:.4rem">En attente de données : ${Object.keys(READY_CONTRIB).filter((k) => !c[k]).map((k) => READY_CONTRIB[k]).join(", ")}.</div>` : ""}</div></div></div>`;
  html += tensionCard(r.tension, r.day);
  html += `<div class="card"><h2>Récupération au fil des jours</h2><div id="c-rd"></div></div>
    <div class="card"><details><summary>Méthode et limites</summary><div class="small">
    <p>Contributeurs inspirés de la Readiness d'Oura (documentation publique) : FC la plus basse vs votre moyenne
    (baisse au-delà de +3 à +5 bpm), équilibre HRV (14 derniers jours pondérés vs moyenne longue), écart de
    température (ne peut que diminuer la note), indice de récupération (optimal ≥ 6 h), sommeil de la nuit, équilibre
    du sommeil sur 14 jours (besoin 8 h), régularité du milieu de sommeil, activité de la veille.</p>
    <p>Barèmes et pondération (moyenne simple) sont <b>locaux</b> : Oura ne publie pas les siens. Le score global
    n'apparaît qu'à partir de 5 contributeurs disponibles.</p>
    <p><b>Signes de tension</b> : FC la plus basse ≥ +5 bpm (et ≥ 2 écarts-types), FC moyenne ≥ +8 bpm, HRV ≤ −25 %,
    température ≥ +0,5 °C, respiration ≥ +2/min, par rapport aux nuits précédentes. « Marqués » = au moins deux signaux
    dont un fort. Outil d'attention, pas de diagnostic.</p></div></details></div>`;
  m.innerHTML = html;
  $("#c-rd", m).append(timeChart({ dayAxis: true, y0: 0, y1: 100, height: 160,
    series: [{ name: "Récupération", color: "var(--good)", type: "dots", r: 4, opacity: 1, points: days.map((x) => [dayNoon(x.day), x.score]) },
      { name: "", color: "var(--good)", points: days.map((x) => [dayNoon(x.day), x.score]), gap: 86400 * 3, width: 1.5 }],
    legend: false, empty: "pas encore de score (5 contributeurs nécessaires)" }));
}

async function pageActivity(m, date) {
  date = date || today();
  const a = await api("activity?date=" + date);
  const s = a.summary;
  const c = (s && s.contributors) || {};
  const t0 = new Date(date + "T00:00:00").getTime() / 1000, t1 = t0 + 86400;
  let html = `<div class="card"><div class="row" style="justify-content:space-between">
      ${dayNav("#/activite", date, shiftDay(date, -1), date < today() ? shiftDay(date, 1) : null)}
      ${s && s.partial ? `<span class="badge">journée en cours ou incomplète</span>` : ""}</div>
    <div class="row" style="margin-top:.6rem;align-items:flex-start">
      <div>${ring(s ? s.score : null, "Activité", s ? (s.score == null ? "calibrage" : undefined) : "pas de données")}</div>
      <div style="flex:1 1 320px">${Object.keys(ACT_CONTRIB).map((k) => c[k] ? contribRow(ACT_CONTRIB[k], c[k], actContribValue(k, c[k])) : "").join("") || `<div class="muted small">pas de contributeurs pour ce jour</div>`}</div></div></div>`;
  if (s) {
    html += `<div class="card"><h2>Journée</h2><div class="tiles">
      ${tile("Effort modéré + intense", num(s.met_min_mh), "MET-min", "repère Oura : 100 / jour")}
      ${tile("Minutes actives", num(s.low_min + s.medium_min + s.high_min), "min", `léger ${s.low_min} · modéré ${s.medium_min} · intense ${s.high_min}`)}
      ${tile("Temps inactif", dur(s.inactive_min / 60), "", `${pl(s.sedentary_bouts, "période")} > 50 min`, s.inactive_min > 720 ? "up-bad" : "")}
      ${tile("Calories actives", num(s.active_kcal), "kcal", `estimation MET × ${a.weight_kg ? num(a.weight_kg, 0) + " kg (profil)" : "poids par défaut"}`)}
      ${tile("Anneau porté", dur(s.worn_min / 60), "", `dont ${dur(s.sleep_min / 60)} de sommeil`)}
    </div></div>`;
  }
  html += `<div class="card"><h2>Intensité (MET, par minute)</h2><div id="c-met"></div>
      <div class="legend"><span style="--c:var(--met)"><i></i>< 3 léger</span><span style="--c:var(--fair)"><i></i>3–6 modéré</span>
      <span style="--c:var(--bad)"><i></i>≥ 6 intense</span><span style="--c:var(--st-deep)"><i style="opacity:.4"></i>sommeil</span>
      <span style="--c:var(--fair)"><i style="opacity:.4"></i>charge</span></div></div>
    <div class="card"><h2>Fréquence cardiaque</h2><div id="c-dhr"></div></div>`;
  if (a.segments.length) {
    html += `<div class="card"><h2>Tranches d'effort <span class="right small muted">MET ≥ 3 pendant ≥ 5 min</span></h2><div class="scroll"><table class="tbl">
      <tr><th>Début</th><th>Fin</th><th class="n">Durée</th><th class="n">MET moyen</th><th class="n">FC moy / max</th></tr>
      ${a.segments.map((g) => `<tr><td>${hm(g.start)}</td><td>${hm(g.end)}</td><td class="n">${g.min} min</td><td class="n">${num(g.met_avg, 1)}</td>
        <td class="n">${g.hr_mean ?? "—"} / ${g.hr_max ?? "—"}</td></tr>`).join("")}</table></div></div>`;
  }
  html += `<div class="card"><details><summary>Méthode et limites</summary><div class="small">
    <p>Intensité : MET par minute calculés par l'anneau (accéléromètre). Temps inactif = minutes d'éveil, anneau au doigt
    (température cutanée > 30 °C), MET < 1,5. Effort modéré ≥ 3 MET, intense ≥ 6 MET ; « MET-min » = somme des MET de ces
    minutes. Calories actives = (MET − 1) × 3,5 × poids / 200 par minute d'activité : ordre de grandeur seulement.</p>
    <p>Contributeurs inspirés de l'Activity Score d'Oura (seuils publiés : 5–8 h d'inactivité au plus, rappel après 50 min,
    3–4 jours d'entraînement, 2 000 MET-min/semaine, 1–2 jours légers). L'objectif quotidien personnalisé n'est pas calculé.</p>
    </div></details></div>`;
  m.innerHTML = html;
  const shades = [...a.sleep.map(([f, t]) => ({ from: f, to: t, color: "var(--shade-sleep)" })),
    ...(a.rests || []).map(([f, t]) => ({ from: f, to: t, color: "var(--shade-sleep)" })),
    ...a.charging.map(([f, t]) => ({ from: f, to: t, color: "var(--shade-charge)" }))];
  $("#c-met", m).append(timeChart({ x0: t0, x1: t1, y0: 0, height: 170, shades, legend: false,
    series: [{ name: "MET", type: "bar", binW: 60, dec: 1, points: a.met.map((p) => [p.t, p.met]),
      colorOf: (v) => v >= 6 ? "var(--bad)" : v >= 3 ? "var(--fair)" : "var(--met)" }],
    empty: "aucune donnée d'intensité ce jour-là" }));
  $("#c-dhr", m).append(timeChart({ x0: t0, x1: t1, height: 170, shades,
    series: [{ name: "FC diurne (mesures 1 min)", color: "var(--hr)", type: "dots", unit: "bpm", points: a.hr.filter((p) => !p.night).map((p) => [p.t, p.hr]) },
      { name: "FC de nuit (5 min)", color: "var(--hrv)", unit: "bpm", points: a.hr.filter((p) => p.night).map((p) => [p.t, p.hr]) }],
    empty: "aucune mesure de FC ce jour-là" }));
}

let trendDays = 30;
async function pageTrends(m) {
  const t = await api("trends?days=" + trendDays);
  const N = t.nights || [], B = t.baselines || {};
  let html = `<div class="card row"><div class="seg">${[14, 30, 90].map((d) => `<button class="chip ${d === trendDays ? "on" : ""}" data-d="${d}">${d} nuits</button>`).join("")}</div>
    <span class="muted small" style="margin-left:auto">${pl(N.length, "nuit")} disponible${N.length > 1 ? "s" : ""}</span></div>`;
  if (!N.length) { m.innerHTML = html + `<div class="card muted">Pas encore de nuits.</div>`; bindTrend(m); return; }
  const blocks = [["c-score", "Scores (sommeil, récupération)"], ["c-tst", "Sommeil total"], ["c-rhr", "FC la plus basse"],
    ["c-hrv", "HRV moyen"], ["c-tdev", "Écart de température"], ["c-rr", "Respiration (expérimental)"], ["c-act", "Effort modéré + intense (MET-min / jour)"]];
  html += `<div class="grid">${blocks.map(([id, h]) => `<div class="card"><h2>${h}</h2><div id="${id}"></div></div>`).join("")}</div>`;
  html += `<div class="card"><h2>Toutes les nuits</h2><div class="scroll"><table class="tbl"><tr><th>Nuit</th><th class="n">Score</th><th class="n">Récup.</th>
    <th class="n">Sommeil</th><th class="n">Profond</th><th class="n">REM</th><th class="n">Effic.</th><th class="n">FC basse</th><th class="n">HRV</th>
    <th class="n">Resp.</th><th class="n">Temp. δ</th></tr>
    ${N.slice().reverse().map((n) => `<tr><td><a href="#/sommeil/${n.night}">${dayLabel(n.night)}</a></td><td class="n">${num(n.score)}</td>
      <td class="n">${num(n.readiness)}</td><td class="n">${dur(n.total)}</td><td class="n">${dur(n.deep)}</td><td class="n">${dur(n.rem)}</td>
      <td class="n">${num(n.efficiency)} %</td><td class="n">${num(n.hr_min, 1)}</td><td class="n">${num(n.hrv_rmssd)}</td>
      <td class="n">${num(n.resp_rate, 1)}</td><td class="n">${n.temp_dev == null ? "—" : signed(n.temp_dev, 2)}</td></tr>`).join("")}</table></div></div>`;
  m.innerHTML = html;
  bindTrend(m);
  const P = (k) => N.map((n) => [dayNoon(n.night), n[k]]);
  const band = (k) => { const b = B[k]; return b && b.n >= 3 ? [b.mean - b.sd, b.mean + b.sd, "habituel (± 1 écart-type)"] : null; };
  const opt = { dayAxis: true, height: 150 };
  $("#c-score", m).append(timeChart({ ...opt, y0: 0, y1: 100, series: [
    { name: "Sommeil", color: "var(--st-light)", points: P("score"), gap: 86400 * 3 },
    { name: "Récupération", color: "var(--opt)", points: P("readiness"), gap: 86400 * 3 }] }));
  $("#c-tst", m).append(timeChart({ ...opt, y0: 0, band: [7, 9, "repère adulte 7–9 h"], legend: true,
    series: [{ name: "Sommeil total", color: "var(--st-light)", type: "bar", center: true, binW: 86400 * 0.7, fmt: dur, points: P("total") }] }));
  $("#c-rhr", m).append(timeChart({ ...opt, band: band("hr_min"), legend: true, series: [{ name: "FC la plus basse", color: "var(--hr)", unit: "bpm", dec: 1, points: P("hr_min"), gap: 86400 * 3 }] }));
  $("#c-hrv", m).append(timeChart({ ...opt, band: band("hrv_rmssd"), legend: true, series: [{ name: "HRV moyen", color: "var(--hrv)", unit: "ms", points: P("hrv_rmssd"), gap: 86400 * 3 }] }));
  $("#c-tdev", m).append(timeChart({ ...opt, minSpan: 1, band: [-0.5, 0.5, "± 0,5 °C"], legend: true, series: [{ name: "Écart", type: "bar", center: true, binW: 86400 * 0.6,
    unit: "°C", fmt: (v) => signed(v, 2), points: P("temp_dev"), colorOf: (v) => Math.abs(v) >= 0.5 ? "var(--bad)" : "var(--temp)" }], empty: "écart disponible à partir de 3 nuits" }));
  $("#c-rr", m).append(timeChart({ ...opt, minSpan: 3, band: band("resp_rate"), legend: true, series: [{ name: "Respiration", color: "var(--resp)", unit: "/min", dec: 1, points: P("resp_rate"), gap: 86400 * 3 }] }));
  $("#c-act", m).append(timeChart({ ...opt, y0: 0, hline: { v: 100 }, series: [{ name: "MET-min", type: "bar", center: true, binW: 86400 * 0.7, color: "var(--met)",
    points: (t.activity || []).filter((a) => !a.partial).map((a) => [dayNoon(a.day), a.met_min_mh]) }], empty: "pas encore de journée complète" }));
}
function bindTrend(m) {
  m.querySelectorAll("[data-d]").forEach((b) => { b.onclick = () => { trendDays = +b.dataset.d; render(); }; });
}

let TAG_KINDS = null;
function journalCard(day, tags) {
  const kinds = TAG_KINDS || {};
  return `<div class="card"><h2>Journal <span class="right small muted">contexte de la veille (${dayLabel(day)})</span></h2>
    <div class="row">${tags.length ? tags.map((t) => `<span class="badge">${esc(kinds[t.kind] || t.kind)}${t.note ? " · " + esc(t.note) : ""} (${dayLabel(t.day)})</span>`).join(" ")
      : `<span class="muted small">Rien de noté.</span>`}</div>
    <div class="seg" style="margin-top:.6rem" data-tagday="${day}">${Object.entries(kinds).map(([k, v]) => `<button class="chip" data-kind="${k}">+ ${esc(v)}</button>`).join("")}</div></div>`;
}
function bindJournal(root) {
  root.querySelectorAll("[data-tagday] [data-kind]").forEach((b) => {
    b.onclick = async () => {
      const day = b.closest("[data-tagday]").dataset.tagday;
      b.disabled = true;
      try { await post("tags", { day, kind: b.dataset.kind }); render(); } catch (e) { alert(e.message); b.disabled = false; }
    };
  });
}

async function pageJournal(m) {
  const t = await api("tags?days=180");
  TAG_KINDS = t.kinds;
  const byDay = {};
  for (const x of t.tags) (byDay[x.day] ??= []).push(x);
  let html = `<div class="card"><h2>Noter un événement</h2>
    <div class="row"><label class="f">Jour<input type="date" id="tg-day" value="${today()}" max="${today()}"></label>
    <label class="f" style="flex:1 1 220px">Note (facultatif)<input id="tg-note" maxlength="280" placeholder="ex. 2 verres de vin, fièvre 38,5 °C…"></label></div>
    <div class="seg" style="margin-top:.6rem">${Object.entries(t.kinds).map(([k, v]) => `<button class="chip" data-add="${k}">${esc(v)}</button>`).join("")}</div>
    <div class="tiny muted" style="margin-top:.5rem">Le journal aide à interpréter les écarts (par ex. alcool ou maladie → FC nocturne plus haute) ;
    il est transmis au résumé du matin et à l'assistant. Pour une soirée, notez le jour de la soirée (la nuit est datée du lendemain).</div></div>`;
  html += `<div class="card"><h2>Historique</h2>${Object.keys(byDay).length ? Object.entries(byDay).map(([d, xs]) =>
    `<div class="kv"><span>${dayLong(d)}</span><span class="row" style="justify-content:flex-end">${xs.map((x) =>
      `<span class="badge">${esc(t.kinds[x.kind] || x.kind)}${x.note ? " · " + esc(x.note) : ""}
       <a href="#" data-del="${x.id}" title="supprimer" style="margin-left:.3rem">✕</a></span>`).join(" ")}</span></div>`).join("")
    : `<div class="muted small">Aucun tag pour l'instant.</div>`}</div>`;
  m.innerHTML = html;
  m.querySelectorAll("[data-add]").forEach((b) => {
    b.onclick = async () => {
      b.disabled = true;
      try { await post("tags", { day: $("#tg-day").value, kind: b.dataset.add, note: $("#tg-note").value }); render(); }
      catch (e) { alert(e.message); b.disabled = false; }
    };
  });
  m.querySelectorAll("[data-del]").forEach((a) => {
    a.onclick = async (ev) => {
      ev.preventDefault();
      if (!confirm("Supprimer ce tag ?")) return;
      try { await api("tags/" + a.dataset.del, { method: "DELETE" }); render(); } catch (e) { alert(e.message); }
    };
  });
}

const chatLog = [];
async function pageAssistant(m) {
  const b = await api("briefing").catch(() => null);
  m.innerHTML = `<div class="card"><h2>Interroger mes données</h2><div class="chatlog" id="log"></div>
    <div class="row"><input id="chat-in" style="flex:1 1 240px" placeholder="ex. comment se compare ma dernière nuit à la semaine ?">
    <button class="primary" id="chat-send">Envoyer</button><button id="chat-deep" title="modèle plus gros, plus lent">Analyse approfondie</button></div>
    <div class="tiny muted" style="margin-top:.4rem">Modèle local (llama-swap) ; il ne voit que vos données résumées (30 nuits, récupération,
    activité, journal) et peut se tromper. Ce n'est pas un avis médical.</div></div>
    ${b ? `<div class="card"><h2>Dernier résumé du matin <span class="badge">${esc(b.model)}</span></h2><div class="brief">${esc(b.text)}</div>
      <div class="tiny muted">nuit du ${dayLabel(b.night)} · ${dtShort(b.ts)}</div></div>` : ""}`;
  const log = $("#log", m);
  const draw = () => { log.innerHTML = chatLog.map((x) => `<div class="msg ${x.u ? "u" : "a"}">${esc(x.text)}</div>`).join(""); };
  draw();
  const send = async (deep) => {
    const inp = $("#chat-in"), text = inp.value.trim();
    if (!text) return;
    inp.value = "";
    chatLog.push({ u: true, text }, { u: false, text: deep ? "… (analyse approfondie, jusqu'à 2–3 min)" : "…" });
    draw();
    try {
      const r = await post("chat", { message: text, deep });
      chatLog[chatLog.length - 1].text = `${r.reply}\n\n[${r.model}]`;
    } catch (e) { chatLog[chatLog.length - 1].text = "⚠ " + e.message; }
    draw();
  };
  $("#chat-send", m).onclick = () => send(false);
  $("#chat-deep", m).onclick = () => send(true);
  $("#chat-in", m).addEventListener("keydown", (e) => { if (e.key === "Enter") send(false); });
}

let dataTab = "signaux", teleHours = 24, evType = "", evLimit = 100, evAll = false;
async function pageData(m, tab) {
  dataTab = tab || dataTab;
  const tabs = { signaux: "Signaux bruts", evenements: "Événements", sante: "Santé du système" };
  let html = `<div class="card row"><div class="seg">${Object.entries(tabs).map(([k, v]) =>
    `<button class="chip ${k === dataTab ? "on" : ""}" onclick="location.hash='#/donnees/${k}'">${v}</button>`).join("")}</div></div>`;
  if (dataTab === "signaux") {
    const t = await api("telemetry?hours=" + teleHours);
    html += `<div class="card row"><div class="seg">${[3, 6, 24, 72, 168].map((h) =>
      `<button class="chip ${h === teleHours ? "on" : ""}" data-h="${h}">${h < 24 ? h + " h" : h / 24 + " j"}</button>`).join("")}</div>
      <span class="muted small" style="margin-left:auto">horloge ${t.anchored === "sync" ? "synchronisée" : "estimée"}</span></div>
      <div class="card"><h2>Température cutanée <span class="right small muted">capteur cutané, hors charge</span></h2><div id="t-temp"></div></div>
      <div class="card"><h2>Fréquence cardiaque et HRV</h2><div id="t-hr"></div></div>
      <div class="card"><h2>Mouvement par heure</h2><div id="t-mot"></div></div>
      <div class="card"><h2>Rapport R de l'oxymètre <span class="badge warn">brut, non étalonné</span></h2><div id="t-spo"></div>
      <div class="tiny muted">R = (AC/DC)<sub>rouge</sub> / (AC/DC)<sub>infrarouge</sub>, moyenné par minute pendant les mesures de l'anneau.
      Sa conversion en saturation (%) exige un étalonnage propre au capteur, que nous n'avons pas : <b>aucune SpO₂ n'est affichée</b>.
      Seules les variations relatives de R ont un sens.</div></div>
      <div class="card"><details><summary>États de l'anneau (${t.states.length})</summary><div class="scroll" style="max-height:18rem">
      ${t.states.slice().reverse().map((s) => `<div class="kv"><span>${dtShort(s.t)}</span><span>${esc(s.label)}</span></div>`).join("")}</div></details></div>`;
    m.innerHTML = html;
    m.querySelectorAll("[data-h]").forEach((b) => { b.onclick = () => { teleHours = +b.dataset.h; render(); }; });
    const x0 = t.since, x1 = t.now, shades = t.charging.map(([f, to]) => ({ from: f, to, color: "var(--shade-charge)" }));
    $("#t-temp", m).append(timeChart({ x0, x1, shades, minSpan: 1, series: [{ name: "Peau", color: "var(--temp)", unit: "°C", dec: 2, points: t.temp.map((p) => [p.t, p.v]) }] }));
    $("#t-hr", m).append(timeChart({ x0, x1, shades, height: 190, series: [
      { name: "FC (bpm)", color: "var(--hr)", type: "dots", r: 1.6, points: t.hr.map((p) => [p.t, p.hr]) },
      { name: "RMSSD (ms, nuit)", color: "var(--hrv)", points: t.hr.filter((p) => p.rmssd != null).map((p) => [p.t, p.rmssd]) }] }));
    const hh = {};
    for (const p of t.motion) { const k = Math.floor(p.t / 3600) * 3600; hh[k] = (hh[k] || 0) + (p.seconds || 0); }
    $("#t-mot", m).append(timeChart({ x0, x1, y0: 0, series: [{ name: "s actives", color: "var(--motion)", type: "bar", binW: 3600, unit: "s",
      points: Object.entries(hh).map(([k, v]) => [+k, v]) }] }));
    $("#t-spo", m).append(timeChart({ x0, x1, series: [{ name: "R", color: "var(--good)", dec: 3, points: t.spo2.map((p) => [p.t, p.r]) }] }));
    return;
  }
  if (dataTab === "evenements") {
    const e = await api(`events?limit=${evLimit}${evType ? "&type=" + encodeURIComponent(evType) : ""}${evAll ? "&all=1" : ""}`);
    html += `<div class="card row"><select id="ev-type"><option value="">tous les types${evAll ? "" : " (hors diagnostic)"}</option>
      ${e.types.map((x) => `<option value="${esc(x.name)}" ${x.name === evType ? "selected" : ""}>${esc(x.name)} (${x.n})</option>`).join("")}</select>
      <select id="ev-lim">${[100, 300, 500].map((n) => `<option ${n === evLimit ? "selected" : ""}>${n}</option>`).join("")}</select>
      <label class="small"><input type="checkbox" id="ev-all" ${evAll ? "checked" : ""}> inclure le diagnostic firmware</label></div>
      <div class="card"><div class="scroll"><table class="tbl"><tr><th>Heure</th><th>Type</th><th>Contenu décodé</th></tr>
      ${e.events.map((x) => `<tr><td style="white-space:nowrap">${dtShort(x.t)}</td><td>${esc(x.name)}</td><td>${x.decoded
        ? `<details><summary>${esc(JSON.stringify(x.decoded).slice(0, 90))}</summary><pre>${esc(JSON.stringify(x.decoded, null, 1))}</pre></details>` : "<span class='muted'>non décodé</span>"}</td></tr>`).join("")}
      </table></div></div>`;
    m.innerHTML = html;
    $("#ev-type", m).onchange = (ev) => { evType = ev.target.value; render(); };
    $("#ev-lim", m).onchange = (ev) => { evLimit = +ev.target.value; render(); };
    $("#ev-all", m).onchange = (ev) => { evAll = ev.target.checked; render(); };
    return;
  }
  const h = await api("health");
  const p = h.profile || {}, ui = h.user_info || {};
  const dev = h.devices[0];
  html += `<div class="grid"><div class="card"><h2>Appareil</h2>${dev ? `
      <div class="kv"><span>Numéro de série</span><span>${esc(dev.serial)}</span></div>
      <div class="kv"><span>Firmware</span><span>${esc(dev.firmware)} (API ${esc(dev.api_version)})</span></div>` : "aucun"}
      <div class="kv"><span>Horloge</span><span>${h.axis.mode}${h.axis.drift_s ? ` · recalée ${num(h.axis.drift_s)} s` : ""}</span></div>
      ${(h.push || []).map((x) => `<div class="kv"><span>Envoi ${esc(x.source)}</span><span>${ago(x.last_push_unix)} · ${x.events_pushed} évts · ${esc(x.last_status)}</span></div>`).join("")}</div>
    <div class="card"><h2>Fichiers et tables dérivées</h2>
      ${Object.entries(h.files).map(([k, f]) => `<div class="kv"><span>${esc(k)}</span><span>${num(f.size / 1048576, 1)} Mo · ${ago(f.mtime)}</span></div>`).join("")}
      ${Object.entries(h.derived_counts || {}).map(([k, v]) => `<div class="kv"><span>${esc(k)}</span><span>${num(v)}</span></div>`).join("")}</div></div>
    <div class="card"><h2>Profil</h2><div class="row">
      <label class="f">Âge<input id="p-age" type="number" min="10" max="120" value="${p.age_years ?? ""}"></label>
      <label class="f">Taille (cm)<input id="p-h" type="number" min="80" max="230" value="${p.height_cm ?? ""}"></label>
      <label class="f">Poids (kg)<input id="p-w" type="number" step="0.5" min="25" max="300" value="${p.weight_kg ?? ""}"></label>
      <label class="f">Sexe<select id="p-s">${[["male", "homme"], ["female", "femme"], ["unspecified", "—"]].map(([v, l]) =>
        `<option value="${v}" ${(p.sex || "unspecified") === v ? "selected" : ""}>${l}</option>`).join("")}</select></label>
      <button class="primary" id="p-save">Enregistrer</button><span id="p-st" class="small muted"></span></div>
      <div class="tiny muted" style="margin-top:.4rem">Utilisé pour les calories estimées. ${p.updated_unix ? "Mis à jour " + ago(p.updated_unix) + "." : ""}
      Profil stocké dans l'anneau (non modifié) : ${ui.age_years ?? "—"} ans · ${ui.height_cm ?? "—"} cm · ${ui.weight_kg ?? "—"} kg.</div></div>
    <div class="card"><details><summary>Événements en base (${num(h.event_counts.reduce((a, x) => a + x.n, 0))})</summary>
      ${h.event_counts.map((x) => `<div class="kv"><span>${esc(x.name)}</span><span>${num(x.n)}</span></div>`).join("")}</details></div>`;
  m.innerHTML = html;
  $("#p-save", m).onclick = async () => {
    const st = $("#p-st");
    st.textContent = "…";
    try {
      await post("profile", { age_years: $("#p-age").value || null, height_cm: $("#p-h").value || null,
        weight_kg: $("#p-w").value || null, sex: $("#p-s").value });
      st.textContent = "✓ enregistré (pris en compte au prochain calcul)";
    } catch (e) { st.textContent = "⚠ " + e.message; }
  };
}

// ------------------------------------------------------------------ routage

const PAGES = [
  ["aujourdhui", "Aujourd'hui", pageToday], ["sommeil", "Sommeil", pageSleep],
  ["recuperation", "Récupération", pageRecovery], ["activite", "Activité", pageActivity],
  ["tendances", "Tendances", pageTrends], ["journal", "Journal", pageJournal],
  ["assistant", "Assistant", pageAssistant], ["donnees", "Données", pageData],
];

function setFresh(ring) {
  const f = $("#fresh"), p = ring && ring.push && ring.push.phone;
  if (!p) { f.textContent = ""; f.className = "fresh"; return; }
  const age = Date.now() / 1000 - p.last_push_unix;
  f.textContent = "données " + ago(p.last_push_unix);
  f.className = "fresh " + (age < 1800 ? "ok" : age < 7200 ? "warn" : "bad");
}

function parseRoute() {
  const [id, arg] = location.hash.replace(/^#\/?/, "").split("/");
  const page = PAGES.find((p) => p[0] === id) || PAGES[0];
  return { page, arg: arg && decodeURIComponent(arg) };
}

let renderSeq = 0;
async function render() {
  const { page, arg } = parseRoute();
  $("#nav").innerHTML = PAGES.map((p) => `<a href="#/${p[0]}" class="${p === page ? "on" : ""}">${p[1]}</a>`).join("");
  const on = $("#nav a.on");
  if (on) on.scrollIntoView({ block: "nearest", inline: "nearest" });
  const m = $("#main"), seq = ++renderSeq;
  hideTip();
  if (!m.dataset.page || m.dataset.page !== page[0]) m.innerHTML = `<div class="card skel">Chargement…</div>`;
  m.dataset.page = page[0];
  try {
    if (!TAG_KINDS && (page[0] === "sommeil")) TAG_KINDS = (await api("tags?days=1")).kinds;
    const tmp = document.createElement("div");
    await page[2](tmp, arg);
    if (seq !== renderSeq) return;                // navigation plus récente entre-temps
    m.replaceChildren(...tmp.childNodes);
  } catch (e) {
    if (seq === renderSeq) m.innerHTML = `<div class="card"><h2>Erreur</h2><div>${esc(e.message)}</div></div>`;
  }
}

window.addEventListener("hashchange", () => { window.scrollTo(0, 0); render(); });
// re-rendu (graphes à la largeur du conteneur) seulement si la LARGEUR change :
// sur mobile, la barre d'adresse qui se masque au défilement change la hauteur
let rsz, lastW = innerWidth;
window.addEventListener("resize", () => {
  if (Math.abs(innerWidth - lastW) < 40) return;
  lastW = innerWidth;
  clearTimeout(rsz);
  rsz = setTimeout(render, 300);
});
setInterval(() => { if (document.visibilityState === "visible" && parseRoute().page[0] === "aujourdhui") render(); }, 300000);
if (!location.hash) location.replace("#/aujourdhui");
render();
