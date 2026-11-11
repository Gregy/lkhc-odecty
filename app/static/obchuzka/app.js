/* LKHC Obchůzka – offline-first walk app (vanilla JS, no build step).
 *
 * Data flow
 *  - /obchuzka/api/body: points + hints, cached in IndexedDB ("kv"/"points") for offline use.
 *  - Everything the user produces goes to the IndexedDB queue first ("queue" store):
 *      photo  {id=uuid, kind:"photo", blob, point_id, taken_at, source:"walk"|"share", status}
 *      entry  {id=uuid, kind:"entry", payload:{uuid, point_id, taken_at, readings[], photos[]}, status}
 *    and is replayed to the server by sync(). Server writes are idempotent on these uuids,
 *    so a retried request never duplicates anything.
 *  - Drafts (typed values) are persisted per day, so closing the app loses nothing.
 *  - The service worker (sw.js) caches the shell and puts shared files (Web Share Target)
 *    into the same queue.
 */
(function () {
  "use strict";

  const API = "/obchuzka/api";
  const DB_NAME = "lkhc-obchuzka";
  const DB_VERSION = 1;
  const MAX_SIDE = 2000;
  const JPEG_Q = 0.85;
  const POLL_MS = 3000;
  const POLL_MAX_MS = 4 * 60 * 1000;

  // ---------------------------------------------------------------- IndexedDB
  let dbp = null;
  function idb() {
    if (!dbp) {
      dbp = new Promise(function (resolve, reject) {
        const r = indexedDB.open(DB_NAME, DB_VERSION);
        r.onupgradeneeded = function () {
          const db = r.result;
          if (!db.objectStoreNames.contains("kv")) db.createObjectStore("kv");
          if (!db.objectStoreNames.contains("queue")) db.createObjectStore("queue", { keyPath: "id" });
        };
        r.onsuccess = function () { resolve(r.result); };
        r.onerror = function () { reject(r.error); };
      });
    }
    return dbp;
  }
  function req(store, mode, fn) {
    return idb().then(function (db) {
      return new Promise(function (resolve, reject) {
        const t = db.transaction(store, mode);
        const res = fn(t.objectStore(store));
        t.oncomplete = function () { resolve(res && "result" in res ? res.result : undefined); };
        t.onerror = function () { reject(t.error); };
        t.onabort = function () { reject(t.error); };
      });
    });
  }
  const kv = {
    get: function (k) { return req("kv", "readonly", function (s) { return s.get(k); }); },
    set: function (k, v) { return req("kv", "readwrite", function (s) { s.put(v, k); }); },
    del: function (k) { return req("kv", "readwrite", function (s) { s.delete(k); }); },
  };
  const queue = {
    all: function () { return req("queue", "readonly", function (s) { return s.getAll(); }).then(function (a) { return (a || []).sort(function (x, y) { return x.created - y.created; }); }); },
    put: function (item) { return req("queue", "readwrite", function (s) { s.put(item); }); },
    del: function (id) { return req("queue", "readwrite", function (s) { s.delete(id); }); },
  };

  // ---------------------------------------------------------------- helpers
  function uuid() {
    if (crypto.randomUUID) return crypto.randomUUID();
    const b = crypto.getRandomValues(new Uint8Array(16));
    b[6] = (b[6] & 0x0f) | 0x40; b[8] = (b[8] & 0x3f) | 0x80;
    const h = Array.from(b, function (x) { return x.toString(16).padStart(2, "0"); }).join("");
    return h.slice(0, 8) + "-" + h.slice(8, 12) + "-" + h.slice(12, 16) + "-" + h.slice(16, 20) + "-" + h.slice(20);
  }
  function pad(n) { return String(n).padStart(2, "0"); }
  function localIso(d) {
    d = d || new Date();
    return d.getFullYear() + "-" + pad(d.getMonth() + 1) + "-" + pad(d.getDate()) + "T" + pad(d.getHours()) + ":" + pad(d.getMinutes()) + ":" + pad(d.getSeconds());
  }
  function today() { return localIso().slice(0, 10); }
  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) { return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]; });
  }
  function decimalsOf(v) { const s = String(v); return s.indexOf(".") >= 0 ? s.split(".")[1].length : 0; }
  // inline SVG (an emoji may render as an empty box on some Android fonts)
  const CAMERA_SVG = '<svg class="icon" aria-hidden="true" focusable="false" viewBox="0 0 24 24" width="24" height="24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M4 8h3l2-3h6l2 3h3a1 1 0 0 1 1 1v10a1 1 0 0 1-1 1H4a1 1 0 0 1-1-1V9a1 1 0 0 1 1-1z"/><circle cx="12" cy="13.5" r="3.5"/></svg>';

  function fmt(v, dec) {
    if (v === null || v === undefined || v === "") return "–";
    if (dec === undefined) dec = decimalsOf(v);
    return new Intl.NumberFormat("cs-CZ", { minimumFractionDigits: dec, maximumFractionDigits: dec }).format(Number(v));
  }
  function fmtDate(iso) {
    if (!iso) return "–";
    const p = iso.slice(0, 10).split("-");
    return Number(p[2]) + ". " + Number(p[1]) + ". " + p[0];
  }
  function parseNum(raw) {
    if (raw === null || raw === undefined) return null;
    let s = String(raw).replace(/[\s  ]/g, "");
    if (!s) return null;
    if (s.indexOf(",") >= 0 && s.indexOf(".") >= 0) s = s.lastIndexOf(",") > s.lastIndexOf(".") ? s.replace(/\./g, "").replace(",", ".") : s.replace(/,/g, "");
    else s = s.replace(",", ".");
    if (!/^\d+(\.\d{1,3})?$/.test(s)) return NaN;
    return s;
  }
  function plainToInput(s) { return s == null ? "" : String(s).replace(".", ","); }

  // ---------------------------------------------------------------- state
  const body = document.body;
  const S = {
    data: null,
    dataAge: null,
    token: (document.querySelector('meta[name="csrf-token"]') || {}).content || "",
    isAdmin: body.getAttribute("data-admin") === "1",
    online: navigator.onLine,
    authProblem: false,
    syncing: false,
    queue: [],
    drafts: {},
    banner: null,
    lastSync: null,
    polls: {},
    extraction: {}, // photo uuid -> server extraction state
    thumbUrls: {},
    lastReporter: "",
  };
  const app = document.getElementById("app");

  function draftKey() { return "drafts-" + today(); }
  function getDraft(pid) {
    let d = S.drafts[pid];
    if (!d) d = S.drafts[pid] = { entryId: uuid(), values: {}, prefilled: {}, acks: [], photos: [], note: "", saved: false };
    return d;
  }
  function saveDrafts() { return kv.set(draftKey(), S.drafts).catch(function () {}); }

  function points() { return (S.data && S.data.points) || []; }
  function point(pid) { return points().find(function (p) { return p.id === pid; }); }

  function queueFor(pid) {
    return S.queue.filter(function (q) { return (q.kind === "entry" ? q.payload.point_id : q.point_id) === pid && (q.day || "") === today(); });
  }
  function pointState(p) {
    const items = queueFor(p.id).filter(function (q) { return q.kind === "entry"; });
    if (items.some(function (q) { return q.status === "conflict" || q.status === "error"; })) return "attention";
    if (items.some(function (q) { return q.status === "pending"; })) return "queued";
    if (items.some(function (q) { return q.status === "done" && q.skip; })) return "skipped";
    const regs = [];
    p.meters.forEach(function (m) { m.registers.forEach(function (r) { regs.push(m.regs[r] && m.regs[r].today); }); });
    const isToday = S.data && S.data.day === today();
    if (isToday && regs.some(function (t) { return t && t.status === "pending"; })) return "attention"; // needs review at home
    if (isToday && regs.length && regs.every(function (t) { return t; })) return "done";
    if (items.some(function (q) { return q.status === "done"; })) return isToday && regs.some(function (t) { return t; }) ? "partial" : "done";
    if (isToday && p.skipped) return "skipped";
    return "missing";
  }
  const STATE_ICON = { done: "✓", partial: "½", queued: "⏳", skipped: "⤼", attention: "!", missing: "" };
  const STATE_TEXT = { done: "hotovo", partial: "zadán jen některý registr", queued: "čeká na odeslání", skipped: "přeskočeno", attention: "vyžaduje pozornost (varování ke kontrole)", missing: "chybí" };

  // ---------------------------------------------------------------- network
  async function api(path, opts) {
    opts = opts || {};
    const headers = Object.assign({ Accept: "application/json" }, opts.headers || {});
    if (opts.method && opts.method !== "GET") headers["X-CSRF-Token"] = S.token;
    const r = await fetch(path, Object.assign({}, opts, { headers: headers, credentials: "same-origin", redirect: "manual" }));
    if (r.type === "opaqueredirect" || r.status === 401) {
      S.authProblem = true;
      const e = new Error("auth"); e.auth = true; throw e;
    }
    return r;
  }
  async function refreshToken() {
    const r = await api(API + "/stav");
    if (r.ok) {
      const j = await r.json();
      S.token = j.csrf_token;
      S.isAdmin = j.is_admin;
      S.authProblem = false;
      return true;
    }
    return false;
  }

  async function loadData() {
    try {
      const r = await api(API + "/body?den=" + today());
      if (!r.ok) throw new Error("HTTP " + r.status);
      const j = await r.json();
      S.data = j;
      S.dataAge = new Date();
      S.token = j.csrf_token || S.token;
      S.isAdmin = j.is_admin;
      S.authProblem = false;
      await kv.set("points", { data: j, at: S.dataAge.toISOString() });
    } catch (e) {
      if (e.auth) {
        // logged out: forget the cached meter data on this device (the unsent queue stays)
        await kv.del("points").catch(function () {});
        S.data = S.data && S.data.day === today() ? S.data : null;
      } else {
        const c = await kv.get("points").catch(function () { return null; });
        if (c && !S.data) { S.data = c.data; S.dataAge = new Date(c.at); }
      }
    }
  }

  // ---------------------------------------------------------------- image capture
  function loadImage(file) {
    return new Promise(function (resolve, reject) {
      const url = URL.createObjectURL(file);
      const img = new Image();
      img.onload = function () { resolve(img); };
      img.onerror = function () { URL.revokeObjectURL(url); reject(new Error("image")); };
      img.src = url;
    });
  }
  async function downscale(file) {
    let src;
    try { src = await createImageBitmap(file, { imageOrientation: "from-image" }); } catch (e) { src = await loadImage(file); }
    const w = src.width, h = src.height;
    const k = Math.min(1, MAX_SIDE / Math.max(w, h));
    const c = document.createElement("canvas");
    c.width = Math.round(w * k); c.height = Math.round(h * k);
    c.getContext("2d").drawImage(src, 0, 0, c.width, c.height);
    const blob = await new Promise(function (r) { c.toBlob(r, "image/jpeg", JPEG_Q); });
    const t = document.createElement("canvas");
    const kt = 160 / Math.max(c.width, c.height);
    t.width = Math.round(c.width * kt); t.height = Math.round(c.height * kt);
    t.getContext("2d").drawImage(c, 0, 0, t.width, t.height);
    return { blob: blob || file, thumb: t.toDataURL("image/jpeg", 0.7) };
  }
  async function addPhotos(pid, files) {
    const d = getDraft(pid);
    for (const f of files) {
      let out;
      try { out = await downscale(f); } catch (e) { out = { blob: f, thumb: null }; }
      const lm = f.lastModified ? new Date(f.lastModified) : new Date();
      const taken = Math.abs(Date.now() - lm.getTime()) < 7 * 24 * 3600 * 1000 ? lm : new Date();
      const item = {
        id: uuid(), kind: "photo", source: "walk", created: Date.now(), day: today(), status: "pending", tries: 0,
        point_id: pid, taken_at: localIso(taken), filename: f.name || "foto.jpg", blob: out.blob, thumb: out.thumb,
      };
      await queue.put(item);
      d.photos.push(item.id);
    }
    await saveDrafts();
    await refreshQueue();
    render();
    sync();
  }

  // ---------------------------------------------------------------- plausibility (client mirror of server checks)
  function inRange(v, lo, hi) { return lo <= hi ? v >= lo && v <= hi : v >= lo || v <= hi; }
  function localIssues(m, reg, sval) {
    const out = [];
    const h = m.regs[reg] || {};
    if (h.last == null) return out;
    const v = Number(sval), last = Number(h.last), range = Math.pow(10, m.digits);
    let delta = v - last;
    if (delta < 0) {
      if (delta + range <= range / 2) delta += range; // counter rollover
      else out.push({ code: "decrease", severity: "error", message: "Stav je nižší než minule (" + fmt(h.last) + ")." });
    } else if (delta > range / 2) {
      out.push({ code: "jump", severity: "error", message: "Nárůst o " + fmt(delta, 0) + " kWh je nepravděpodobný." });
    }
    if (delta >= 0 && h.high != null && h.low != null) {
      const lo = Number(h.low), hi = Number(h.high);
      if (!inRange(v, lo, hi) && v > hi) out.push({ code: "high", severity: "warning", message: "Spotřeba je výrazně vyšší než obvykle (čekám nejvýš " + fmt(h.high) + ")." });
      if (!inRange(v, lo, hi)) {
        for (let k = 1; k <= 3; k++) {
          const a = v / Math.pow(10, k), b = v * Math.pow(10, k);
          if (inRange(a, lo, hi) || inRange(b, lo, hi)) {
            out.push({ code: "decimal_slip", severity: "warning", message: "Vypadá to na posunutou desetinnou čárku (čekám ≈ " + fmt(h.expected || h.low) + ")." });
            break;
          }
        }
      }
    }
    if (delta >= 0 && last >= 10 && String(Math.trunc(v)).length > String(Math.trunc(last)).length && delta > last) {
      out.push({ code: "magnitude", severity: "warning", message: "Hodnota má o řád více číslic než minule." });
    }
    return out;
  }
  async function serverIssues(m, reg, sval) {
    if (!navigator.onLine) return null;
    try {
      const ctl = new AbortController();
      const t = setTimeout(function () { ctl.abort(); }, 3000);
      const r = await api(API + "/kontrola?meter_id=" + m.id + "&register=" + reg + "&value=" + encodeURIComponent(sval) + "&den=" + today(), { signal: ctl.signal });
      clearTimeout(t);
      if (!r.ok) return null;
      return (await r.json()).issues;
    } catch (e) { return null; }
  }

  // ---------------------------------------------------------------- sync
  async function refreshQueue() { S.queue = await queue.all().catch(function () { return []; }); updatePill(); }

  async function sendPhoto(it) {
    const fd = new FormData();
    fd.append("file", it.blob, it.filename || "foto.jpg");
    fd.append("uuid", it.id);
    fd.append("source", it.source || "walk");
    fd.append("filename", it.filename || "");
    if (it.taken_at) fd.append("taken_at", it.taken_at);
    if (it.point_id) fd.append("point_id", String(it.point_id));
    if (it.reported_by) fd.append("reported_by", it.reported_by);
    return post(API + "/foto", fd, it);
  }
  async function sendEntry(it) { return post(API + "/zaznam", JSON.stringify(it.payload), it, "application/json"); }

  async function post(url, bodyData, it, ctype, retried) {
    let r;
    try {
      const headers = ctype ? { "Content-Type": ctype } : {};
      r = await api(url, { method: "POST", body: bodyData, headers: headers });
    } catch (e) {
      return "stop"; // offline or logged out: keep the item, try later
    }
    if (r.status === 403 && !retried) {
      if (await refreshToken().catch(function () { return false; })) return post(url, bodyData, it, ctype, true);
    }
    let j = {};
    try { j = await r.json(); } catch (e) { j = {}; }
    it.tries = (it.tries || 0) + 1;
    if (r.ok) {
      it.status = "done";
      it.result = j;
      it.error = null;
      if (it.kind === "photo") { delete it.blob; it.server_id = j.id; }
      if (it.kind === "entry" && j.status === "pending") it.note_after = "uloženo, ale server našel varování – potvrďte doma v Kontrole";
      await queue.put(it);
      return "ok";
    }
    if (r.status === 409 && it.kind === "entry") {
      it.status = "conflict";
      it.result = j;
      it.error = (j.results || []).filter(function (x) { return x.status === "conflict"; }).map(function (x) { return "už zapsáno " + fmt(x.existing); }).join(", ") || j.message;
      await queue.put(it);
      return "ok";
    }
    if (r.status === 403) { it.status = "error"; it.error = "Nemáte oprávnění zapisovat (role čtenář?)."; await queue.put(it); return "stop"; }
    if (r.status >= 400 && r.status < 500) { it.status = "error"; it.error = j.error || j.message || ("chyba " + r.status); await queue.put(it); return "ok"; }
    it.error = "server " + r.status + ", zkusím znovu"; await queue.put(it);
    return "stop";
  }

  async function sync() {
    if (S.syncing) return;
    if (!navigator.onLine) { updatePill(); return; }
    S.syncing = true;
    updatePill();
    try {
      await refreshQueue();
      const pend = S.queue.filter(function (q) { return q.status === "pending"; });
      let stopped = false;
      for (const it of pend.filter(function (q) { return q.kind === "photo"; })) {
        if ((await sendPhoto(it)) === "stop") { stopped = true; break; }
        if (it.status === "done" && it.source === "walk") watchExtraction(it.id);
      }
      if (!stopped) {
        await refreshQueue();
        const waiting = new Set(S.queue.filter(function (q) { return q.kind === "photo" && q.status === "pending"; }).map(function (q) { return q.id; }));
        for (const it of S.queue.filter(function (q) { return q.kind === "entry" && q.status === "pending"; })) {
          if ((it.payload.photos || []).some(function (u) { return waiting.has(u); })) continue;
          if ((await sendEntry(it)) === "stop") { stopped = true; break; }
        }
      }
      if (!stopped) {
        S.lastSync = new Date();
        await pruneQueue();
        const anyDoneToday = S.queue.some(function (q) { return q.kind === "entry" && q.status === "done"; });
        if (anyDoneToday) await loadData();
      }
    } finally {
      S.syncing = false;
      await refreshQueue();
      render();
    }
  }
  async function pruneQueue() {
    const cutoff = Date.now() - 3 * 24 * 3600 * 1000;
    for (const q of S.queue) if (q.status === "done" && q.created < cutoff) await queue.del(q.id);
  }

  function updatePill() {
    const pill = document.querySelector("[data-queue-pill]");
    const net = document.querySelector("[data-net]");
    S.online = navigator.onLine;
    if (net) { net.textContent = S.online ? "online" : "offline"; net.className = "net " + (S.online ? "online" : "offline"); }
    if (!pill) return;
    const pend = S.queue.filter(function (q) { return q.status === "pending"; }).length;
    const bad = S.queue.filter(function (q) { return q.status === "error" || q.status === "conflict"; }).length;
    const held = S.queue.filter(function (q) { return q.status === "held"; }).length;
    pill.textContent = S.syncing ? "Odesílám…" : (pend ? "Fronta: " + pend : "Fronta: odesláno") + (held ? " · " + held + " sdílené" : "") + (bad ? " · " + bad + " ⚠" : "");
    pill.className = "queue-pill" + (pend ? " has-items" : "") + (bad ? " has-errors" : "");
  }

  // ---------------------------------------------------------------- extraction polling → prefill
  function watchExtraction(photoId) {
    if (S.polls[photoId] || !(S.data && S.data.extraction)) return;
    const started = Date.now();
    S.polls[photoId] = true;
    async function tick() {
      if (Date.now() - started > POLL_MAX_MS) { delete S.polls[photoId]; return; }
      if (!navigator.onLine) { setTimeout(tick, POLL_MS * 3); return; }
      try {
        const r = await api(API + "/foto/" + photoId);
        const j = r.ok ? await r.json() : { status: "unknown" };
        S.extraction[photoId] = j;
        if (j.status === "queued") { setTimeout(tick, POLL_MS); renderIfPoint(); return; }
        delete S.polls[photoId];
        if (j.status === "done") applyPrefill(photoId, j);
        renderIfPoint();
      } catch (e) { setTimeout(tick, POLL_MS * 2); }
    }
    setTimeout(tick, 800);
  }
  function applyPrefill(photoId, j) {
    const it = S.queue.find(function (q) { return q.id === photoId; });
    const pid = it && it.point_id;
    if (!pid) return;
    const d = getDraft(pid);
    Object.keys(j.values || {}).forEach(function (mid) {
      Object.keys(j.values[mid]).forEach(function (reg) {
        const v = j.values[mid][reg];
        const key = mid + ":" + reg;
        d.fromPhoto = d.fromPhoto || {};
        d.fromPhoto[key] = v;
        if (v.conflict) return;
        if (!d.values[key]) { d.values[key] = plainToInput(v.value); d.prefilled[key] = true; d.saved = false; }
      });
    });
    saveDrafts();
  }

  // ---------------------------------------------------------------- views
  function route() {
    const h = location.hash || "#/";
    let m = h.match(/^#\/bod\/(\d+)/);
    if (m) return { view: "point", pid: Number(m[1]) };
    if (h.indexOf("#/fronta") === 0) return { view: "queue" };
    return { view: "list" };
  }
  function renderIfPoint() { if (route().view === "point") render(); else updatePill(); }

  function bannerHtml() {
    let out = "";
    if (S.banner) out += '<div class="alert alert-' + S.banner.kind + ' walk-banner">' + S.banner.html + "</div>";
    if (S.authProblem) out += '<div class="alert alert-warning walk-banner">Nejste přihlášen(a) – zadané hodnoty zůstávají v telefonu. <a href="/login?next=/obchuzka/">Přihlásit se</a> a pak se vše odešle.</div>';
    if (!S.online) out += '<div class="alert alert-info walk-banner">Offline – vše se ukládá do telefonu a odešle se, až bude signál.' + (S.dataAge ? " Data míst z " + S.dataAge.toLocaleString("cs-CZ") + "." : "") + "</div>";
    if (!S.isAdmin && S.data) out += '<div class="alert alert-info walk-banner">Jste přihlášen(a) jako čtenář – odečty můžete jen prohlížet.</div>';
    return out;
  }

  function heldHtml() {
    const held = S.queue.filter(function (q) { return q.status === "held"; });
    if (!held.length) return "";
    let html = '<section class="card held-box"><h2>Sdílené fotky (' + held.length + ")</h2>" +
      '<p class="small">Fotky přišly přes „Sdílet“ (např. z WhatsAppu). Odeslat je ke kontrole? Datum odečtu potvrdíte v Kontrole.</p><div class="shots">';
    held.forEach(function (q) {
      if (!S.thumbUrls[q.id] && q.blob) S.thumbUrls[q.id] = URL.createObjectURL(q.blob);
      html += '<div class="shot"><img src="' + (S.thumbUrls[q.id] || "") + '" alt=""><span class="shot-st">' + esc(q.filename || "") + "</span></div>";
    });
    html += '</div><div class="field"><label for="w-rep">Nahlásil</label><input id="w-rep" data-held-rep value="' + esc(S.lastReporter || "") + '" placeholder="např. Novák (WhatsApp)"></div>' +
      '<div class="walk-actions"><button type="button" class="btn btn-primary" data-held-send' + (S.isAdmin ? "" : " disabled") + '>Odeslat ke kontrole</button>' +
      '<button type="button" class="btn btn-danger" data-held-drop>Zahodit</button></div></section>';
    return html;
  }

  function renderList() {
    const ps = points();
    if (!ps.length) {
      app.innerHTML = bannerHtml() + heldHtml() + '<p class="muted">' + (S.authProblem ? "" : "Seznam míst není k dispozici – připojte se k internetu a obnovte stránku.") + "</p>";
      return;
    }
    const states = ps.map(pointState);
    const done = states.filter(function (s) { return s !== "missing"; }).length;
    const firstMissing = ps.find(function (p, i) { return states[i] === "missing"; });
    let html = bannerHtml() + heldHtml();
    html += '<div class="progress-line"><span><strong>Obchůzka ' + esc(fmtDate(today())) + "</strong></span><span>hotovo " + done + " / " + ps.length + "</span></div>";
    html += '<ul class="points">';
    ps.forEach(function (p, i) {
      const st = states[i];
      const m0 = p.meters[0];
      const r0 = m0 && m0.regs[m0.registers[0]];
      let sub = p.meters.map(function (m) { return m.serial; }).join(", ");
      if (r0 && r0.today) sub += " · dnes " + fmt(r0.today.value);
      else if (r0 && r0.last) sub += " · minule " + fmt(r0.last) + " (" + fmtDate(r0.last_date) + ")";
      html += '<li><a class="point-item ' + st + '" href="#/bod/' + p.id + '"><span class="st" aria-label="' + STATE_TEXT[st] + '">' + STATE_ICON[st] + "</span>" +
        '<span class="pi-body"><span class="pi-name">' + esc(p.name) + '</span><span class="pi-sub">' + esc(sub) + "</span></span><span aria-hidden=\"true\">›</span></a></li>";
    });
    html += "</ul>";
    html += '<div class="walk-bottom">' + (firstMissing ? '<a class="btn btn-primary" href="#/bod/' + firstMissing.id + '">Další chybějící: ' + esc(firstMissing.name) + "</a>" : '<a class="btn" href="/odecty/kontrola">Vše hotovo · Kontrola</a>') + "</div>";
    app.innerHTML = html;
  }

  function regHint(m, reg) {
    const h = m.regs[reg] || {};
    const parts = [];
    if (h.today) parts.push("dnes už zapsáno " + fmt(h.today.value) + (h.today.status === "pending" ? " (nepotvrzeno)" : ""));
    if (h.last != null) parts.push("minule " + fmt(h.last) + " (" + fmtDate(h.last_date) + ")");
    if (h.expected != null) parts.push("čekám ≈ " + fmt(h.expected, 0) + (h.high != null ? " (" + fmt(h.low, 0) + "–" + fmt(h.high, 0) + ")" : ""));
    return parts.join(" · ");
  }

  function renderPoint(pid) {
    const p = point(pid);
    if (!p) { location.hash = "#/"; return; }
    const ps = points();
    const idx = ps.indexOf(p);
    const prev = ps[idx - 1], next = ps[idx + 1];
    const d = getDraft(pid);
    const shots = queueFor(pid).filter(function (q) { return q.kind === "photo"; });
    const entry = queueFor(pid).filter(function (q) { return q.kind === "entry"; }).pop();
    let html = bannerHtml();
    html += '<div class="pt-nav">' + (prev ? '<a class="btn" href="#/bod/' + prev.id + '">‹ ' + esc(prev.name) + "</a>" : '<a class="btn" href="#/">‹ Seznam</a>') +
      (next ? '<a class="btn" href="#/bod/' + next.id + '">' + esc(next.name) + " ›</a>" : '<a class="btn" href="#/">Seznam ›</a>') + "</div>";
    html += '<div class="pt-head"><h1>' + esc(p.name) + "</h1>" + (p.description ? '<p class="muted small">' + esc(p.description) + "</p>" : "") + "</div>";
    if (entry) {
      const txt = { pending: "⏳ uloženo v telefonu, čeká na odeslání", done: "✓ odesláno" + (entry.note_after ? " – " + entry.note_after : ""), conflict: "⚠ konflikt: " + (entry.error || ""), error: "⚠ chyba: " + (entry.error || "") }[entry.status];
      html += '<div class="alert alert-' + (entry.status === "done" ? "success" : entry.status === "pending" ? "info" : "error") + '">' + esc(txt) + "</div>";
    }
    if (S.isAdmin) {
      html += '<label class="btn btn-primary camera-btn">' + CAMERA_SVG + 'Vyfotit elektroměr<input type="file" accept="image/*" capture="environment" multiple hidden data-camera></label>';
    }
    if (shots.length) {
      html += '<div class="shots">';
      shots.forEach(function (s) {
        const ex = S.extraction[s.id];
        let st = s.status === "pending" ? "čeká na odeslání" : s.status === "error" ? "chyba" : "odesláno";
        if (ex) st = ex.status === "queued" ? "rozpoznávám…" : ex.status === "done" ? (Object.keys(ex.values || {}).length ? "rozpoznáno" : "nerozpoznáno") : ex.status === "failed" ? "nerozpoznáno" : st;
        html += '<div class="shot">' + (s.thumb ? '<img src="' + s.thumb + '" alt="">' : "") + '<span class="shot-st">' + esc(st) + "</span></div>";
      });
      html += "</div>";
      shots.forEach(function (s) {
        const ex = S.extraction[s.id];
        (ex && ex.mismatch || []).forEach(function (msg) { html += '<div class="alert alert-warning small" data-mismatch><strong>Pozor:</strong> ' + esc(msg) + "</div>"; });
      });
    }
    p.meters.forEach(function (m) {
      html += '<section class="meter-box"><h2>' + esc(m.serial) + (p.meters.length > 1 ? "" : "") + "</h2>";
      if (m.display_note) html += '<p class="small muted">' + esc(m.display_note) + "</p>";
      m.registers.forEach(function (reg) {
        const key = m.id + ":" + reg;
        const val = d.values[key] || "";
        const fp = d.fromPhoto && d.fromPhoto[key];
        html += '<div class="reg-field"><label for="v-' + m.id + "-" + reg + '"><span>' + esc(reg) + "</span>" + (m.decimals_shown ? '<span class="small muted">' + m.decimals_shown + " des. místa</span>" : "") + "</label>";
        html += '<input id="v-' + m.id + "-" + reg + '" data-key="' + key + '" inputmode="decimal" autocomplete="off" enterkeyhint="next" value="' + esc(val) + '" placeholder="' +
          esc(m.regs[reg] && m.regs[reg].expected != null ? "≈ " + fmt(m.regs[reg].expected, 0) : "stav kWh") + '"' + (d.prefilled[key] ? ' class="prefilled"' : "") + (S.isAdmin ? "" : " disabled") + ">";
        if (fp) {
          html += '<div class="from-photo">' + (fp.conflict ? "fotka ukazuje víc hodnot – zadejte ručně" : "z fotky: " + esc(fmt(fp.value)) + (d.prefilled[key] ? " – zkontrolujte" : (val && parseNum(val) !== fp.value ? ' <button type="button" class="btn-link" data-use="' + key + '" data-val="' + esc(fp.value) + '">použít</button>' : ""))) +
            (fp.flags && fp.flags.length ? " (" + esc(fp.flags.join(", ")) + ")" : "") + "</div>";
        }
        html += '<div class="reg-hint">' + esc(regHint(m, reg)) + "</div></div>";
      });
      html += "</section>";
    });
    html += '<div data-issues></div>';
    if (S.isAdmin) {
      html += '<div class="field"><label for="w-note">Poznámka</label><input id="w-note" data-note value="' + esc(d.note) + '"></div>';
      html += '<div class="walk-actions"><button type="button" class="btn btn-primary" data-save="next">Uložit a další ›</button><button type="button" class="btn" data-save="stay">Uložit</button></div>';
      html += '<details class="skip-box"><summary>Nepřístupné / přeskočit</summary><div class="field"><label for="w-skip">Důvod</label><input id="w-skip" data-skipnote placeholder="např. zamčeno"></div><button type="button" class="btn" data-skip>Označit jako přeskočené</button></details>';
    }
    app.innerHTML = html;
  }

  function renderQueue() {
    let html = bannerHtml() + "<h1>Fronta odesílání</h1>";
    const items = S.queue.slice().reverse();
    if (!items.length) html += '<p class="muted">Fronta je prázdná.</p>';
    html += '<ul class="queue-list">';
    items.forEach(function (q) {
      const p = point(q.kind === "entry" ? q.payload.point_id : q.point_id);
      const what = q.kind === "photo" ? (q.source === "share" ? "sdílená fotka " + esc(q.filename || "") : "fotka – " + esc(p ? p.name : "?")) :
        (q.skip ? "přeskočeno – " : "odečet – ") + esc(p ? p.name : "?") + (q.payload.readings ? " " + q.payload.readings.map(function (r) { return r.register + " " + fmt(r.value); }).join(", ") : "");
      const st = { pending: "čeká", done: "odesláno", error: "chyba", conflict: "konflikt", held: "čeká na potvrzení (seznam míst)" }[q.status] || q.status;
      html += '<li class="' + q.status + '"><div><strong>' + st + "</strong> · " + what + ' <span class="muted small">' + esc((q.taken_at || (q.payload && q.payload.taken_at) || "").replace("T", " ")) + "</span></div>";
      if (q.error) html += '<div class="small">' + esc(q.error) + "</div>";
      if (q.note_after) html += '<div class="small">' + esc(q.note_after) + "</div>";
      html += '<div class="q-actions">';
      if (q.status === "error") html += '<button class="btn" data-retry="' + q.id + '">Zkusit znovu</button>';
      if (q.status === "conflict") html += '<button class="btn btn-primary" data-overwrite="' + q.id + '">Přepsat hodnotu na serveru</button>';
      if (q.status !== "pending" || q.kind === "photo") html += '<button class="btn btn-danger" data-delete="' + q.id + '">Odstranit z telefonu</button>';
      html += "</div></li>";
    });
    html += "</ul>";
    html += '<div class="walk-actions"><button class="btn btn-primary" data-sync>Odeslat teď</button><a class="btn" href="#/">Zpět na seznam</a></div>';
    if (S.lastSync) html += '<p class="muted small">Naposledy odesláno ' + S.lastSync.toLocaleTimeString("cs-CZ") + "</p>";
    app.innerHTML = html;
  }

  function render() {
    updatePill();
    const r = route();
    if (r.view === "point") {
      // keep focus/caret when re-rendering the same point
      const active = document.activeElement && document.activeElement.getAttribute("data-key");
      renderPoint(r.pid);
      if (active) { const el = app.querySelector('[data-key="' + active + '"]'); if (el) { el.focus(); const n = el.value.length; try { el.setSelectionRange(n, n); } catch (e) { /* ignore */ } } }
    } else if (r.view === "queue") renderQueue();
    else renderList();
  }

  // ---------------------------------------------------------------- actions
  async function saveEntry(pid, goNext) {
    const p = point(pid);
    const d = getDraft(pid);
    const readings = [];
    const problems = [];
    const issuesBox = app.querySelector("[data-issues]");
    for (const m of p.meters) {
      for (const reg of m.registers) {
        const key = m.id + ":" + reg;
        const raw = d.values[key];
        if (!raw) continue;
        const v = parseNum(raw);
        if (v === null) continue;
        if (Number.isNaN(v)) { problems.push({ key: key, html: esc(m.serial + " " + reg + ": „" + raw + "“ není číslo (max 3 desetinná místa).") }); continue; }
        let iss = localIssues(m, reg, v);
        const srv = await serverIssues(m, reg, v);
        if (srv) iss = srv;
        readings.push({ meter_id: m.id, register: reg, value: v, acks: [], _issues: iss, _key: key });
      }
    }
    if (!readings.length && !problems.length) problems.push({ html: "Zadejte alespoň jednu hodnotu (nebo místo označte jako přeskočené)." });
    // require explicit acknowledgement of every issue
    const pendingAcks = [];
    readings.forEach(function (r) {
      r._issues.forEach(function (i) {
        const id = r._key + ":" + i.code;
        if (d.acks.indexOf(id) >= 0) r.acks.push(i.code);
        else pendingAcks.push({ id: id, issue: i, r: r });
      });
    });
    if (problems.length || pendingAcks.length) {
      let html = problems.map(function (x) { return '<div class="alert alert-error">' + x.html + "</div>"; }).join("");
      pendingAcks.forEach(function (x) {
        html += '<label class="ack ack-' + x.issue.severity + '"><input type="checkbox" data-ack="' + esc(x.id) + '"><span><strong>' + esc(x.r._key.split(":")[1]) + ":</strong> " + esc(x.issue.message) + (x.issue.link && /^\/sprava\//.test(x.issue.link) ? ' <a href="' + esc(x.issue.link) + '" target="_blank" rel="noopener">' + esc(x.issue.link_label || "Nastavení elektroměru") + "</a>" : "") + " <em>Hodnota je správně.</em></span></label>";
      });
      if (pendingAcks.length) html += '<p class="small muted">Zkontrolujte hodnotu na displeji. Když je správně, zaškrtněte a uložte znovu.</p>';
      issuesBox.innerHTML = html;
      issuesBox.scrollIntoView({ block: "center", behavior: "smooth" });
      return;
    }
    const payload = {
      uuid: d.entryId, point_id: pid, taken_at: localIso(), note: d.note || "",
      readings: readings.map(function (r) { return { meter_id: r.meter_id, register: r.register, value: r.value, acks: r.acks }; }),
      photos: d.photos.slice(),
    };
    // same entry uuid for re-saves of this point today: the server updates instead of duplicating
    await queue.put({ id: d.entryId, kind: "entry", created: Date.now(), day: today(), status: "pending", tries: 0, payload: payload });
    d.saved = true;
    await saveDrafts();
    await refreshQueue();
    if (goNext) {
      const ps = points();
      const after = ps.slice(ps.indexOf(p) + 1).concat(ps.slice(0, ps.indexOf(p)));
      const nxt = after.find(function (x) { return pointState(x) === "missing"; });
      location.hash = nxt ? "#/bod/" + nxt.id : "#/";
    } else render();
    window.scrollTo(0, 0);
    sync();
  }

  async function skipPoint(pid, note) {
    const id = uuid();
    await queue.put({ id: id, kind: "entry", skip: true, created: Date.now(), day: today(), status: "pending", tries: 0,
      payload: { uuid: id, point_id: pid, taken_at: localIso(), skip: true, skip_note: note || "" } });
    await refreshQueue();
    const ps = points();
    const p = point(pid);
    const nxt = ps.slice(ps.indexOf(p) + 1).find(function (x) { return pointState(x) === "missing"; });
    location.hash = nxt ? "#/bod/" + nxt.id : "#/";
    sync();
  }

  app.addEventListener("input", function (e) {
    const key = e.target.getAttribute("data-key");
    const r = route();
    if (r.view !== "point") return;
    const d = getDraft(r.pid);
    if (key) { d.values[key] = e.target.value; d.prefilled[key] = false; e.target.classList.remove("prefilled"); d.saved = false; }
    if (e.target.hasAttribute("data-note")) d.note = e.target.value;
    saveDrafts();
  });
  app.addEventListener("change", function (e) {
    const r = route();
    if (e.target.hasAttribute("data-camera") && e.target.files && e.target.files.length) {
      addPhotos(r.pid, Array.from(e.target.files));
      e.target.value = "";
    }
    const ack = e.target.getAttribute("data-ack");
    if (ack && r.view === "point") {
      const d = getDraft(r.pid);
      if (e.target.checked) { if (d.acks.indexOf(ack) < 0) d.acks.push(ack); } else d.acks = d.acks.filter(function (a) { return a !== ack; });
      saveDrafts();
    }
  });
  app.addEventListener("keydown", function (e) {
    if (e.key === "Enter" && e.target.getAttribute("data-key")) {
      const inputs = Array.from(app.querySelectorAll("[data-key]"));
      const i = inputs.indexOf(e.target);
      if (inputs[i + 1]) inputs[i + 1].focus();
      else { const b = app.querySelector('[data-save="next"]'); if (b) b.click(); }
      e.preventDefault();
    }
  });
  app.addEventListener("click", async function (e) {
    const t = e.target;
    const r = route();
    if (t.closest("[data-save]")) { saveEntry(r.pid, t.closest("[data-save]").getAttribute("data-save") === "next"); return; }
    if (t.closest("[data-skip]")) { const n = app.querySelector("[data-skipnote]"); skipPoint(r.pid, n ? n.value : ""); return; }
    if (t.closest("[data-use]")) {
      const b = t.closest("[data-use]");
      const d = getDraft(r.pid);
      d.values[b.getAttribute("data-use")] = plainToInput(b.getAttribute("data-val"));
      d.prefilled[b.getAttribute("data-use")] = true;
      saveDrafts(); render(); return;
    }
    if (t.closest("[data-sync]")) { sync(); return; }
    if (t.closest("[data-held-send]")) {
      const repEl = app.querySelector("[data-held-rep]");
      const rep = repEl ? repEl.value.trim() : "";
      S.lastReporter = rep;
      kv.set("last_reporter", rep).catch(function () {});
      for (const q of S.queue.filter(function (x) { return x.status === "held"; })) { q.status = "pending"; q.reported_by = rep; await queue.put(q); }
      S.banner = { kind: "info", html: "Sdílené fotky se odesílají ke kontrole – potvrdíte je v <a href=\"/odecty/kontrola\">Kontrole</a>." };
      await refreshQueue(); render(); sync(); return;
    }
    if (t.closest("[data-held-drop]")) {
      if (!window.confirm("Zahodit sdílené fotky?")) return;
      for (const q of S.queue.filter(function (x) { return x.status === "held"; })) await queue.del(q.id);
      await refreshQueue(); render(); return;
    }
    const retry = t.closest("[data-retry]");
    if (retry) { const it = S.queue.find(function (q) { return q.id === retry.getAttribute("data-retry"); }); if (it) { it.status = "pending"; it.error = null; await queue.put(it); sync(); } return; }
    const ow = t.closest("[data-overwrite]");
    if (ow) {
      const it = S.queue.find(function (q) { return q.id === ow.getAttribute("data-overwrite"); });
      if (it && window.confirm("Přepsat hodnotu uloženou na serveru hodnotou z telefonu?")) {
        it.payload.overwrite = true; it.status = "pending"; it.error = null; await queue.put(it); sync();
      }
      return;
    }
    const del = t.closest("[data-delete]");
    if (del && window.confirm("Odstranit z telefonu? Neodeslaná data se ztratí.")) { await queue.del(del.getAttribute("data-delete")); await refreshQueue(); render(); }
  });

  // ---------------------------------------------------------------- boot
  async function boot() {
    const params = new URLSearchParams(location.search);
    if (params.get("sdileni") === "nezpracovano") {
      S.banner = { kind: "warning", html: "Sdílení fotek se nepodařilo převzít (aplikace ještě nebyla připravena). Zkuste sdílet znovu, nebo fotky nahrajte přes <a href=\"/odecty/davka\">Dávku fotek</a>." };
    }
    if (params.has("sdileno") || params.has("sdileni")) history.replaceState(null, "", location.pathname + location.hash);
    try {
      S.drafts = (await kv.get(draftKey())) || {};
    } catch (e) {
      S.drafts = {};
      S.banner = { kind: "error", html: "Telefon nepovolil úložiště (IndexedDB) – offline fronta nefunguje, hodnoty se odesílají jen online." };
    }
    await refreshQueue();
    S.lastReporter = (await kv.get("last_reporter").catch(function () { return ""; })) || "";
    const cached = await kv.get("points").catch(function () { return null; });
    if (cached) { S.data = cached.data; S.dataAge = new Date(cached.at); }
    render();
    await loadData();
    render();
    sync();
    S.queue.filter(function (q) { return q.kind === "photo" && q.status === "done" && q.source === "walk" && q.day === today(); }).forEach(function (q) { watchExtraction(q.id); });
    if ("serviceWorker" in navigator) {
      navigator.serviceWorker.register("/obchuzka/sw.js", { scope: "/obchuzka/" }).catch(function (e) { console.warn("SW", e); });
      navigator.serviceWorker.addEventListener("message", function (ev) { if (ev.data && ev.data.type === "shared") { refreshQueue().then(render); } });
    }
  }

  window.addEventListener("hashchange", render);
  window.addEventListener("online", function () { updatePill(); loadData().then(render); sync(); });
  window.addEventListener("offline", function () { render(); });
  document.addEventListener("visibilitychange", function () { if (!document.hidden) sync(); });
  setInterval(sync, 20000);
  boot();

  window.lkhcWalk = { state: S, sync: sync, queue: queue }; // for debugging / tests
})();
