/* LKHC Odečty – shared client helpers (no build step).
 *
 * CSRF: the token is in <meta name="csrf-token">.
 *  - every HTMX request gets the X-CSRF-Token header,
 *  - every same-origin fetch() with an unsafe method gets it too (global fetch is wrapped),
 *  - every <form method="post"> gets a hidden csrf_token input (also after HTMX swaps).
 * So templates/JS normally need to do nothing. Use lkhc.csrfToken() if you build requests by hand
 * (e.g. in a service worker sync, read it from the page and send it as X-CSRF-Token).
 */
(function () {
  "use strict";

  const UNSAFE = new Set(["POST", "PUT", "PATCH", "DELETE"]);

  function csrfToken() {
    const m = document.querySelector('meta[name="csrf-token"]');
    return m ? m.getAttribute("content") : "";
  }

  function isSameOrigin(url) {
    try {
      return new URL(url, window.location.href).origin === window.location.origin;
    } catch (e) {
      return false;
    }
  }

  // --- fetch wrapper -------------------------------------------------------
  const nativeFetch = window.fetch.bind(window);
  window.fetch = function (input, init) {
    init = init || {};
    const method = (init.method || (input instanceof Request ? input.method : "GET")).toUpperCase();
    const url = input instanceof Request ? input.url : String(input);
    if (UNSAFE.has(method) && isSameOrigin(url)) {
      const headers = new Headers(init.headers || (input instanceof Request ? input.headers : undefined));
      if (!headers.has("X-CSRF-Token")) headers.set("X-CSRF-Token", csrfToken());
      init = Object.assign({}, init, { headers: headers, credentials: init.credentials || "same-origin" });
    }
    return nativeFetch(input, init);
  };

  // --- forms ----------------------------------------------------------------
  function injectCsrf(root) {
    const token = csrfToken();
    if (!token) return;
    (root || document).querySelectorAll('form[method="post" i], form[method="POST"]').forEach(function (form) {
      if (form.action && !isSameOrigin(form.action)) return;
      let input = form.querySelector('input[name="csrf_token"]');
      if (!input) {
        input = document.createElement("input");
        input.type = "hidden";
        input.name = "csrf_token";
        form.appendChild(input);
      }
      input.value = token;
    });
  }

  // --- HTMX -----------------------------------------------------------------
  document.addEventListener("htmx:configRequest", function (evt) {
    evt.detail.headers["X-CSRF-Token"] = csrfToken();
  });
  document.addEventListener("htmx:load", function (evt) {
    injectCsrf(evt.target);
  });

  // --- small UI helpers -----------------------------------------------------
  document.addEventListener("click", function (evt) {
    const dismiss = evt.target.closest("[data-dismiss]");
    if (dismiss) {
      const box = dismiss.closest(".flash, .alert");
      if (box) box.remove();
      return;
    }
    const confirmEl = evt.target.closest("[data-confirm]");
    if (confirmEl && !window.confirm(confirmEl.getAttribute("data-confirm"))) {
      evt.preventDefault();
      evt.stopPropagation();
    }
  }, true);

  // --- formatting -------------------------------------------------------------
  const fmtCache = {};
  function fmtNumber(value, decimals) {
    if (value === null || value === undefined || value === "") return "–";
    decimals = decimals || 0;
    if (!fmtCache[decimals]) {
      fmtCache[decimals] = new Intl.NumberFormat("cs-CZ", { minimumFractionDigits: decimals, maximumFractionDigits: decimals });
    }
    const n = Number(value);
    // same as the server-side |num filter: proper minus sign, never "-0"
    const s = fmtCache[decimals].format(n === 0 ? 0 : n).replace("-", "\u2212");
    return /^\u2212[0,\s\u00a0]*$/.test(s) ? s.slice(1) : s;
  }
  function fmtKwh(value, decimals) {
    const s = fmtNumber(value, decimals);
    return s === "–" ? s : s + " kWh";
  }
  // Dates like the server-side |date_cs / |datetime_cs / |month_cs filters: "3.7.2026", "3.7.2026 14:05",
  // "7.2026" (no spaces – the browser's cs-CZ locale formatting would give "3. 7. 2026"). Accepts a Date, ms or
  // an ISO string.
  function toDate(value) {
    if (value === null || value === undefined || value === "") return null;
    const d = value instanceof Date ? value : new Date(value);
    return isNaN(d.getTime()) ? null : d;
  }
  function pad2(n) { return String(n).padStart(2, "0"); }
  function fmtDate(value) {
    const d = toDate(value);
    return d ? d.getDate() + "." + (d.getMonth() + 1) + "." + d.getFullYear() : "–";
  }
  function fmtDateTime(value) {
    const d = toDate(value);
    return d ? fmtDate(d) + " " + pad2(d.getHours()) + ":" + pad2(d.getMinutes()) : "–";
  }
  function fmtMonth(value) {
    const d = toDate(value);
    return d ? (d.getMonth() + 1) + "." + d.getFullYear() : "–";
  }

  // --- tenant colours (stable across the app; see base.html) ---------------------
  function isDark() {
    return window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches;
  }
  function tenantColors() {
    const el = document.getElementById("tenant-colors");
    let data = {};
    try { data = el ? JSON.parse(el.textContent) : {}; } catch (e) { data = {}; }
    const dark = isDark();
    const out = {};
    Object.keys(data).forEach(function (id) {
      out[id] = Object.assign({}, data[id], { color: dark ? data[id].dark : data[id].light });
    });
    return out; // { "<tenant id>": {id, name, light, dark, color, is_remainder} }
  }
  function cssVar(name) {
    return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  }

  window.lkhc = {
    csrfToken: csrfToken,
    fetch: window.fetch,
    fmtNumber: fmtNumber,
    fmtKwh: fmtKwh,
    fmtDate: fmtDate,
    fmtDateTime: fmtDateTime,
    fmtMonth: fmtMonth,
    tenantColors: tenantColors,
    isDark: isDark,
    cssVar: cssVar,
  };

  // --- logout: forget the walk app's offline copies on this device --------------------
  // The /obchuzka service worker keeps the page shell (user name, CSRF token) and the meter list for
  // offline use; after an explicit logout neither may be served to the next person using the phone.
  // (The unsent reading/photo queue is kept on purpose - it is data that has not reached the server.)
  function forgetOfflineData() {
    const jobs = [];
    if (window.caches) {
      jobs.push(caches.keys().then(function (keys) {
        return Promise.all(keys.filter(function (k) { return k.indexOf("lkhc-obchuzka-") === 0; })
          .map(function (k) { return caches.delete(k); }));
      }));
    }
    if (window.indexedDB) {
      jobs.push(new Promise(function (resolve) {
        const r = indexedDB.open("lkhc-obchuzka");
        r.onupgradeneeded = function () { r.transaction.abort(); }; // DB did not exist: do not create it
        r.onerror = function () { resolve(); };
        r.onsuccess = function () {
          const db = r.result;
          if (!db.objectStoreNames.contains("kv")) { db.close(); resolve(); return; }
          const t = db.transaction("kv", "readwrite");
          t.objectStore("kv").delete("points");
          t.oncomplete = t.onerror = t.onabort = function () { db.close(); resolve(); };
        };
      }));
    }
    // never block the logout for long
    return Promise.race([Promise.all(jobs), new Promise(function (r) { setTimeout(r, 1500); })]);
  }
  document.addEventListener("submit", function (evt) {
    const form = evt.target;
    if (!(form instanceof HTMLFormElement) || form.getAttribute("action") !== "/logout" || form.dataset.cleaned) return;
    evt.preventDefault();
    form.dataset.cleaned = "1";
    forgetOfflineData().catch(function () {}).then(function () { form.submit(); });
  });

  document.addEventListener("DOMContentLoaded", function () {
    injectCsrf(document);
  });
})();
