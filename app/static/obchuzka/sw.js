/* LKHC Obchůzka – service worker. Scope: /obchuzka/ (served from /obchuzka/sw.js).
 *
 * - Shell (/obchuzka/): network-first; the cached copy is used only when the network fails.
 *   A redirect (= logged out) deletes the cached shell, so no stale page is served after logout.
 * - Static assets of the walk app: stale-while-revalidate from a versioned cache.
 * - Nothing else is intercepted or cached (other sections, APIs, photos go straight to the network).
 * - Web Share Target: the OS share sheet POSTs multipart files to /obchuzka/sdilet. They are put
 *   into the app's IndexedDB queue with status "held" and the app is opened; the user then
 *   explicitly confirms "Odeslat ke kontrole" (which uploads with session + CSRF token). A forged
 *   cross-site POST can therefore at most show unexpected photos in the app – it cannot upload.
 */
"use strict";

const CACHE_VERSION = "v4";
const CACHE = "lkhc-obchuzka-" + CACHE_VERSION;
const SHELL = "/obchuzka/";
const ASSETS = [
  "/static/css/app.css",
  "/static/css/capture.css",
  "/static/obchuzka/app.js",
  "/static/obchuzka/icon-192.png",
  "/static/obchuzka/icon-512.png",
  "/obchuzka/manifest.webmanifest",
];
const SHARE_PATH = "/obchuzka/sdilet";

self.addEventListener("install", function (event) {
  event.waitUntil(caches.open(CACHE).then(function (c) { return c.addAll(ASSETS); }).then(function () { return self.skipWaiting(); }));
});

self.addEventListener("activate", function (event) {
  event.waitUntil(
    caches.keys().then(function (keys) {
      return Promise.all(keys.filter(function (k) { return k.indexOf("lkhc-obchuzka-") === 0 && k !== CACHE; }).map(function (k) { return caches.delete(k); }));
    }).then(function () { return self.clients.claim(); })
  );
});

function isShellRequest(url) {
  return url.pathname === SHELL;
}

async function shell(request) {
  const cache = await caches.open(CACHE);
  try {
    const res = await fetch(request);
    if (res.type === "opaqueredirect" || res.redirected || res.status === 401 || res.status === 403) {
      await cache.delete(SHELL); // logged out / no access: never serve the old page again
    } else if (res.ok) {
      await cache.put(SHELL, res.clone());
    }
    return res;
  } catch (e) {
    const cached = await cache.match(SHELL);
    if (cached) return cached;
    return new Response(
      "<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width'>" +
      "<title>Offline</title><p style='font-family:system-ui;padding:16px'>Jste offline a obchůzka není v tomto telefonu " +
      "k dispozici (ještě nebyla otevřena se signálem, nebo jste se odhlásili). Otevřete ji jednou online, pak bude fungovat i bez signálu.</p>",
      { status: 503, headers: { "Content-Type": "text/html; charset=utf-8" } }
    );
  }
}

async function asset(request) {
  const cache = await caches.open(CACHE);
  const cached = await cache.match(request, { ignoreSearch: true });
  const network = fetch(request).then(function (res) {
    if (res.ok) cache.put(request, res.clone());
    return res;
  }).catch(function () { return null; });
  return cached || (await network) || new Response("", { status: 504 });
}

// --- share target -----------------------------------------------------------------
const DB_NAME = "lkhc-obchuzka";
function idb() {
  return new Promise(function (resolve, reject) {
    const r = indexedDB.open(DB_NAME, 1);
    r.onupgradeneeded = function () {
      const db = r.result;
      if (!db.objectStoreNames.contains("kv")) db.createObjectStore("kv");
      if (!db.objectStoreNames.contains("queue")) db.createObjectStore("queue", { keyPath: "id" });
    };
    r.onsuccess = function () { resolve(r.result); };
    r.onerror = function () { reject(r.error); };
  });
}
function pad(n) { return String(n).padStart(2, "0"); }
function today() { const d = new Date(); return d.getFullYear() + "-" + pad(d.getMonth() + 1) + "-" + pad(d.getDate()); }

async function handleShare(event) {
  try {
    const form = await event.request.formData();
    const files = form.getAll("photos").filter(function (f) { return f && typeof f === "object" && f.size > 0 && /^image\//.test(f.type || "image/"); });
    if (!files.length) return Response.redirect("/obchuzka/?sdileni=nezpracovano", 303);
    const db = await idb();
    await new Promise(function (resolve, reject) {
      const t = db.transaction("queue", "readwrite");
      const s = t.objectStore("queue");
      files.slice(0, 30).forEach(function (f, i) {
        s.put({
          id: self.crypto.randomUUID(), kind: "photo", source: "share", status: "held", created: Date.now() + i, day: today(),
          tries: 0, point_id: null, taken_at: null, filename: (f.name || "sdileno.jpg").slice(0, 200), blob: f,
        });
      });
      t.oncomplete = resolve;
      t.onerror = function () { reject(t.error); };
    });
    const clients = await self.clients.matchAll({ type: "window" });
    clients.forEach(function (c) { c.postMessage({ type: "shared", count: files.length }); });
    return Response.redirect("/obchuzka/?sdileno=" + files.length, 303);
  } catch (e) {
    return Response.redirect("/obchuzka/?sdileni=nezpracovano", 303);
  }
}

self.addEventListener("fetch", function (event) {
  const req = event.request;
  const url = new URL(req.url);
  if (url.origin !== self.location.origin) return;
  if (req.method === "POST" && url.pathname === SHARE_PATH) {
    event.respondWith(handleShare(event));
    return;
  }
  if (req.method !== "GET") return;
  if (req.mode === "navigate" && isShellRequest(url)) {
    event.respondWith(shell(req));
    return;
  }
  if (ASSETS.indexOf(url.pathname) >= 0) {
    event.respondWith(asset(req));
  }
  // everything else: not intercepted (no caching of other sections, APIs or photos)
});
