/* LKHC Odečty – capture pages (review queue, readings grid, batch upload, photo detail).
 * Vanilla JS, no build step. CSRF: app.js adds the header to fetch()/HTMX automatically. */
(function () {
  "use strict";

  // Validation re-renders come back as 422 (and stale cards as 409): htmx 2 does not swap 4xx by
  // default, which would hide the error messages – swap them like a normal response.
  if (window.htmx && htmx.config && Array.isArray(htmx.config.responseHandling)) {
    htmx.config.responseHandling.unshift({ code: "409|422", swap: true, error: false });
  }

  // --- photo viewer: zoom / rotate / pan (dialog and inline variants) -------------------
  function makeViewer(root) {
    const stage = root.querySelector("[data-viewer-stage]");
    const img = root.querySelector("[data-viewer-img]");
    const st = { rot: 0, scale: 1, x: 0, y: 0 };
    function apply() {
      img.style.transform = "translate(" + st.x + "px," + st.y + "px) rotate(" + st.rot + "deg) scale(" + st.scale + ")";
    }
    function reset() { st.rot = 0; st.scale = 1; st.x = 0; st.y = 0; apply(); }
    function zoom(f) { st.scale = Math.min(8, Math.max(0.5, st.scale * f)); apply(); }
    root.addEventListener("click", function (e) {
      const b = e.target.closest("[data-viewer-act]");
      if (!b) return;
      const act = b.getAttribute("data-viewer-act");
      if (act === "rotl") st.rot -= 90;
      else if (act === "rotr") st.rot += 90;
      else if (act === "in") return zoom(1.4);
      else if (act === "out") return zoom(1 / 1.4);
      else if (act === "reset") return reset();
      else if (act === "close") { if (root.close) root.close(); return; }
      apply();
    });
    stage.addEventListener("wheel", function (e) { e.preventDefault(); zoom(e.deltaY < 0 ? 1.15 : 1 / 1.15); }, { passive: false });
    // pointer drag + two-finger pinch
    const pts = new Map();
    let last = null, pinch = null;
    stage.addEventListener("pointerdown", function (e) { stage.setPointerCapture(e.pointerId); pts.set(e.pointerId, [e.clientX, e.clientY]); last = [e.clientX, e.clientY]; });
    stage.addEventListener("pointermove", function (e) {
      if (!pts.has(e.pointerId)) return;
      pts.set(e.pointerId, [e.clientX, e.clientY]);
      if (pts.size === 2) {
        const [a, b] = Array.from(pts.values());
        const d = Math.hypot(a[0] - b[0], a[1] - b[1]);
        if (pinch) zoom(d / pinch);
        pinch = d;
        return;
      }
      if (last) { st.x += e.clientX - last[0]; st.y += e.clientY - last[1]; last = [e.clientX, e.clientY]; apply(); }
    });
    function up(e) { pts.delete(e.pointerId); if (pts.size < 2) pinch = null; last = null; }
    stage.addEventListener("pointerup", up);
    stage.addEventListener("pointercancel", up);
    stage.addEventListener("dblclick", function () { if (st.scale === 1) zoom(2.5); else reset(); });
    return { img: img, reset: reset };
  }

  document.querySelectorAll("[data-inline-viewer]").forEach(makeViewer);

  const dlg = document.getElementById("photo-viewer");
  let dlgViewer = null;
  document.addEventListener("click", function (e) {
    const opener = e.target.closest("[data-viewer-src]");
    if (!opener || !dlg) return;
    e.preventDefault();
    if (!dlgViewer) dlgViewer = makeViewer(dlg);
    dlgViewer.img.src = opener.getAttribute("data-viewer-src");
    dlg.querySelector("[data-viewer-title]").textContent = opener.getAttribute("data-viewer-title") || "";
    dlg.querySelector("[data-viewer-orig]").href = opener.getAttribute("data-viewer-src");
    dlgViewer.reset();
    if (dlg.showModal) dlg.showModal(); else dlg.setAttribute("open", "");
  });

  // --- review: candidate chips fill inputs; add / remove rows -------------------------------
  document.addEventListener("click", function (e) {
    const fill = e.target.closest("[data-fill-target]");
    if (fill) {
      const input = document.querySelector(fill.getAttribute("data-fill-target"));
      if (input) {
        input.value = fill.getAttribute("data-fill-value").replace(".", input.type === "date" ? "." : ",");
        input.classList.remove("conflict");
        input.dispatchEvent(new Event("input", { bubbles: true }));
        input.focus();
      }
      return;
    }
    const add = e.target.closest("[data-add-row]");
    if (add) {
      const form = add.closest("form");
      const tpl = form.querySelector("template[data-row-template]");
      const key = "n" + Date.now().toString(36);
      const holder = document.createElement("div");
      holder.innerHTML = tpl.innerHTML.split("__KEY__").join(key);
      form.querySelector("[data-newrows]").appendChild(holder.firstElementChild);
      return;
    }
    const rm = e.target.closest("[data-remove-row]");
    if (rm) { rm.closest("[data-row]").remove(); return; }
    if (e.target.closest("[data-close-editor]")) {
      const ed = document.getElementById("cell-editor");
      if (ed) ed.innerHTML = "";
    }
  });

  // --- readings grid: keyboard activation and reload when a new row appears -----------------
  document.addEventListener("keydown", function (e) {
    if ((e.key === "Enter" || e.key === " ") && e.target.matches && e.target.matches("td.editable")) {
      e.preventDefault();
      e.target.click();
    }
  });
  document.addEventListener("htmx:afterSwap", function (e) {
    if (!e.target || e.target.id !== "cell-editor") return;
    const panel = e.target.querySelector("[data-cell-id]");
    if (!panel) return;
    panel.scrollIntoView({ block: "nearest", behavior: "smooth" });
    if (panel.querySelector(".alert-success") && !document.getElementById(panel.getAttribute("data-cell-id"))) {
      window.location.reload(); // a reading on a new date: the grid needs a new row
    }
  });

  // --- batch upload: one request per file, with progress -------------------------------------
  const form = document.querySelector("[data-batch-upload]");
  if (form && window.fetch && window.FormData) {
    form.addEventListener("submit", async function (e) {
      const input = form.querySelector('input[type="file"]');
      const files = Array.from(input.files || []);
      if (!files.length) return;
      e.preventDefault();
      const box = form.querySelector("[data-progress]");
      const list = form.querySelector("[data-progress-list]");
      const summary = form.querySelector("[data-progress-summary]");
      const btn = form.querySelector('button[type="submit"]');
      box.hidden = false;
      btn.disabled = true;
      list.innerHTML = "";
      const items = files.map(function (f) {
        const li = document.createElement("li");
        li.textContent = f.name + " – čeká";
        list.appendChild(li);
        return li;
      });
      let ok = 0, bad = 0, dup = 0;
      const reported = form.querySelector('[name="reported_by"]').value;
      for (let i = 0; i < files.length; i++) {
        summary.textContent = "Nahrávám " + (i + 1) + " / " + files.length + "…";
        items[i].textContent = files[i].name + " – nahrávám…";
        const fd = new FormData();
        fd.append("files", files[i], files[i].name);
        fd.append("reported_by", reported);
        let res = null;
        for (let attempt = 0; attempt < 3 && !res; attempt++) {
          try {
            const r = await fetch(form.action, { method: "POST", body: fd, headers: { Accept: "application/json" } });
            if (r.status === 401 || r.status === 403) { res = { ok: false, error: "Nejste přihlášen(a) nebo vypršela relace – obnovte stránku." }; break; }
            const j = await r.json();
            res = (j.results && j.results[0]) || { ok: false, error: "Neočekávaná odpověď serveru." };
          } catch (err) {
            await new Promise(function (rs) { setTimeout(rs, 1500 * (attempt + 1)); });
          }
        }
        res = res || { ok: false, error: "Síťová chyba." };
        if (res.ok) {
          ok++;
          if (res.duplicate || !res.created) dup++;
          items[i].innerHTML = "";
          const a = document.createElement("a");
          a.href = res.url; a.textContent = files[i].name;
          items[i].appendChild(a);
          items[i].appendChild(document.createTextNode(
            (res.created ? " – ✓ " : " – už nahráno ") + (res.taken_at ? "pořízeno " + lkhc.fmtDateTime(res.taken_at) : "bez času pořízení (datum zadáte v kontrole)")));
          items[i].className = res.taken_at ? "ok" : "warn";
        } else {
          bad++;
          items[i].textContent = files[i].name + " – ✗ " + res.error;
          items[i].className = "err";
        }
      }
      summary.textContent = "Hotovo: " + ok + " nahráno" + (dup ? " (" + dup + " už dříve)" : "") + (bad ? ", " + bad + " chyb" : "") + ".";
      form.querySelector("[data-progress-done]").hidden = ok === 0;
      btn.disabled = false;
      input.value = "";
    });
  }
})();
