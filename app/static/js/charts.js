/* LKHC Odečty – charts for the reports pages (Chart.js 4, vendored) + quality popovers.
 *
 * Data comes from <script type="application/json"> blocks rendered by the server.
 * Values with quality are objects {v: number, q: "exact"|"estimated"|"provisional", t: "provenance text"}.
 * Only provisional (extrapolated) values are marked ("≈", hatched bars, provenance in the tooltip);
 * interpolated ("estimated") values are shown like exact ones.
 * Colours: tenants use lkhc.tenantColors() (stable across the app), everything else uses CSS tokens.
 * Charts re-render when the colour scheme changes (light/dark).
 */
(function () {
  "use strict";

  var registry = []; // [{build: fn, chart: Chart}]

  function css(name, fallback) {
    var v = window.lkhc ? lkhc.cssVar(name) : "";
    return v || fallback;
  }
  function tokens() {
    return {
      text: css("--text", "#0b0b0b"),
      text2: css("--text-2", "#52514e"),
      muted: css("--muted", "#6b6a66"),
      grid: css("--border", "#dcdbd6"),
      surface: css("--surface", "#fcfcfb"),
      accent: css("--accent", "#2a78d6"),
    };
  }
  function fmt(v, d) { return window.lkhc ? lkhc.fmtNumber(v, d || 0) : String(v); }
  // "3.7.2026" / "7.2026" (no spaces, unlike the cs-CZ locale formatting); same as lkhc.fmtDate / lkhc.fmtMonth
  function fmtDate(ms) { var d = new Date(ms); return d.getDate() + "." + (d.getMonth() + 1) + "." + d.getFullYear(); }
  function fmtMonth(ms) { var d = new Date(ms); return (d.getMonth() + 1) + "." + d.getFullYear(); }
  function readJSON(id) {
    var el = document.getElementById(id);
    if (!el) return null;
    try { return JSON.parse(el.textContent); } catch (e) { return null; }
  }
  function alpha(hex, a) {
    var h = (hex || "#888888").replace("#", "");
    if (h.length === 3) h = h.split("").map(function (c) { return c + c; }).join("");
    var n = parseInt(h, 16);
    return "rgba(" + ((n >> 16) & 255) + "," + ((n >> 8) & 255) + "," + (n & 255) + "," + a + ")";
  }
  function tenantColor(id) {
    var all = window.lkhc ? lkhc.tenantColors() : {};
    return (all[String(id)] && all[String(id)].color) || "#888888";
  }
  function isMarked(p) { return !!p && p.q === "provisional"; }
  function qmark(p) { return isMarked(p) ? "≈" : ""; }
  function compactTick(v) {
    var a = Math.abs(v);
    if (a >= 10000) return fmt(v / 1000, 0) + " tis.";
    return fmt(v, 0);
  }
  function isNarrow() { return window.matchMedia && window.matchMedia("(max-width: 600px)").matches; }

  function baseOptions(t) {
    return {
      responsive: true,
      maintainAspectRatio: false,
      animation: false,
      interaction: { mode: "index", intersect: false },
      plugins: {
        legend: { display: false },
        tooltip: {
          backgroundColor: t.surface,
          titleColor: t.text,
          bodyColor: t.text,
          footerColor: t.muted,
          borderColor: t.grid,
          borderWidth: 1,
          padding: 10,
          boxWidth: 12,
          boxHeight: 3,
          usePointStyle: false,
          bodyFont: { size: 13 },
          footerFont: { size: 11, weight: "normal" },
        },
      },
      scales: {
        x: {
          grid: { display: false },
          border: { color: t.grid },
          ticks: { color: t.text2, maxRotation: 0, autoSkip: true },
        },
        y: {
          grid: { color: t.grid, lineWidth: 1 },
          border: { display: false },
          ticks: { color: t.text2, callback: compactTick, maxTicksLimit: 6 },
          title: { display: !isNarrow(), text: "kWh", color: t.muted },
        },
      },
    };
  }

  // Legend: coloured mark + text in text tokens (identity never by colour alone).
  function renderLegend(legendId, items) {
    var box = document.getElementById(legendId);
    if (!box) return;
    box.textContent = "";
    items.forEach(function (it) {
      var span = document.createElement("span");
      span.className = "legend-item";
      var sw = document.createElement("span");
      sw.className = "legend-swatch" + (it.line ? " legend-line" : "") + (it.hatch ? " legend-hatch" : "");
      if (!it.hatch) sw.style.background = it.color;
      if (it.line) { sw.style.background = "transparent"; sw.style.borderTopColor = it.color; }
      span.appendChild(sw);
      span.appendChild(document.createTextNode(it.label));
      box.appendChild(span);
    });
  }

  function hasProvisional(lists) {
    return lists.some(function (l) { return l.some(function (p) { return p && p.q === "provisional"; }); });
  }
  var HATCH_ITEM = { label: "předběžné (šrafováno)", hatch: true };

  function register(build) {
    var entry = { build: build, chart: null };
    entry.chart = build();
    registry.push(entry);
  }
  function rebuildAll() {
    registry.forEach(function (e) {
      if (e.chart) e.chart.destroy();
      e.chart = e.build();
    });
  }
  if (window.matchMedia) {
    var mq = window.matchMedia("(prefers-color-scheme: dark)");
    if (mq.addEventListener) mq.addEventListener("change", rebuildAll);
  }

  // Provisional bars get a diagonal hatch of their own colour (a state, distinct from any series
  // lightness), the tooltip and the tables say so as well.
  var hatchCache = {};
  function hatch(color) {
    if (hatchCache[color]) return hatchCache[color];
    var c = document.createElement("canvas");
    c.width = 8; c.height = 8;
    var g = c.getContext("2d");
    g.globalAlpha = 0.28;
    g.fillStyle = color;
    g.fillRect(0, 0, 8, 8);
    g.globalAlpha = 1;
    g.strokeStyle = color;
    g.lineWidth = 2.5;
    g.beginPath();
    g.moveTo(-2, 10); g.lineTo(10, -2);
    g.moveTo(-2, 2); g.lineTo(2, -2);
    g.moveTo(6, 10); g.lineTo(10, 6);
    g.stroke();
    hatchCache[color] = g.createPattern(c, "repeat");
    return hatchCache[color];
  }
  function qColors(values, color) {
    return values.map(function (p) { return p && p.q === "provisional" ? hatch(color) : color; });
  }
  function qTooltipLine(name, p, unit) {
    if (!p) return name + ": –";
    return name + ": " + qmark(p) + fmt(p.v, 0) + (unit ? " kWh" : "") + (isMarked(p) ? " (předběžně)" : "");
  }
  function provenanceFooter(points) {
    var lines = [];
    points.forEach(function (p) {
      if (isMarked(p) && p.t) {
        p.t.split("\n").slice(1, 3).forEach(function (l) { if (lines.indexOf(l) < 0) lines.push(l); });
      }
    });
    if (lines.length > 4) lines = lines.slice(0, 4).concat(["…"]);
    return lines;
  }

  /* Dashboard: stacked monthly columns per tenant (current year) + previous year's total as a line. */
  function monthlyStacked(canvasId, dataId, legendId) {
    var data = readJSON(dataId);
    var canvas = document.getElementById(canvasId);
    if (!data || !canvas || !window.Chart) return;
    register(function () {
      var t = tokens();
      var datasets = data.series.map(function (s) {
        var c = tenantColor(s.id);
        return {
          type: "bar",
          label: s.name,
          data: s.values.map(function (p) { return p ? p.v : null; }),
          _q: s.values,
          backgroundColor: qColors(s.values, c),
          hoverBackgroundColor: c,
          borderColor: t.surface,
          borderWidth: { top: 2, bottom: 0, left: 0, right: 0 },
          borderSkipped: false,
          maxBarThickness: 24,
          stack: "cur",
          order: 2,
        };
      });
      var hasPrev = data.prev_total.some(function (p) { return p; });
      if (hasPrev) {
        datasets.push({
          type: "line",
          label: "Celkem " + data.prev_year,
          data: data.prev_total.map(function (p) { return p ? p.v : null; }),
          _q: data.prev_total,
          borderColor: t.muted,
          backgroundColor: t.muted,
          borderWidth: 2,
          pointRadius: 4,
          pointHoverRadius: 5,
          pointBorderColor: t.surface,
          pointBorderWidth: 2,
          spanGaps: false,
          tension: 0,
          order: 1,
        });
      }
      var opts = baseOptions(t);
      opts.scales.x.stacked = true;
      opts.scales.y.stacked = true;
      opts.plugins.tooltip.callbacks = {
        title: function (items) { return items.length ? items[0].label + " " + data.year : ""; },
        label: function (ctx) {
          var p = ctx.dataset._q[ctx.dataIndex];
          if (!p) return null;
          return qTooltipLine(ctx.dataset.label, p, true);
        },
        footer: function (items) {
          var i = items.length ? items[0].dataIndex : -1;
          var tot = data.total[i];
          var lines = [];
          if (tot) lines.push("Celkem " + data.year + ": " + qmark(tot) + fmt(tot.v, 0) + " kWh");
          return lines.concat(provenanceFooter(items.map(function (it) { return it.dataset._q[it.dataIndex]; })));
        },
      };
      opts.plugins.tooltip.filter = function (item) { return item.raw !== null && item.raw !== undefined; };
      renderLegend(legendId, data.series.map(function (s) { return { label: s.name, color: tenantColor(s.id) }; })
        .concat(hasPrev ? [{ label: "celkem " + data.prev_year, color: t.muted, line: true }] : [])
        .concat(hasProvisional(data.series.map(function (x) { return x.values; })) ? [HATCH_ITEM] : []));
      return new Chart(canvas, { data: { labels: data.labels, datasets: datasets }, options: opts });
    });
  }

  /* Tenant / point page: monthly bars (one colour) + previous year as a line. data: {labels, cur:[], prev:[], year, prev_year, color_id|color} */
  function monthlyBars(canvasId, dataId, legendId) {
    var data = readJSON(dataId);
    var canvas = document.getElementById(canvasId);
    if (!data || !canvas || !window.Chart) return;
    register(function () {
      var t = tokens();
      var c = data.tenant_id ? tenantColor(data.tenant_id) : t.accent;
      var datasets = [{
        type: "bar",
        label: String(data.year),
        data: data.cur.map(function (p) { return p ? p.v : null; }),
        _q: data.cur,
        backgroundColor: qColors(data.cur, c),
        hoverBackgroundColor: c,
        borderRadius: { topLeft: 4, topRight: 4, bottomLeft: 0, bottomRight: 0 },
        borderSkipped: "start",
        maxBarThickness: 24,
        order: 2,
      }];
      var hasPrev = data.prev && data.prev.some(function (p) { return p; });
      if (hasPrev) {
        datasets.push({
          type: "line",
          label: String(data.prev_year),
          data: data.prev.map(function (p) { return p ? p.v : null; }),
          _q: data.prev,
          borderColor: t.muted, backgroundColor: t.muted, borderWidth: 2,
          pointRadius: 4, pointHoverRadius: 5, pointBorderColor: t.surface, pointBorderWidth: 2,
          tension: 0, order: 1,
        });
      }
      var opts = baseOptions(t);
      opts.plugins.tooltip.callbacks = {
        label: function (ctx) {
          var p = ctx.dataset._q[ctx.dataIndex];
          return p ? qTooltipLine(ctx.dataset.label, p, true) : null;
        },
        footer: function (items) { return provenanceFooter(items.map(function (it) { return it.dataset._q[it.dataIndex]; })); },
      };
      opts.plugins.tooltip.filter = function (item) { return item.raw !== null && item.raw !== undefined; };
      renderLegend(legendId, [{ label: String(data.year), color: c }]
        .concat(hasPrev ? [{ label: String(data.prev_year), color: t.muted, line: true }] : [])
        .concat(hasProvisional([data.cur]) ? [HATCH_ITEM] : []));
      return new Chart(canvas, { data: { labels: data.labels, datasets: datasets }, options: opts });
    });
  }

  /* Point page: monthly consumption stacked T1/T2. data: {labels, series:[{label, values:[q]}], tenant_id} */
  function registerBars(canvasId, dataId, legendId) {
    var data = readJSON(dataId);
    var canvas = document.getElementById(canvasId);
    if (!data || !canvas || !window.Chart) return;
    register(function () {
      var t = tokens();
      var base = data.tenant_id ? tenantColor(data.tenant_id) : t.accent;
      var colors = [base, alpha(base, 0.55)];
      var datasets = data.series.map(function (s, k) {
        var c = colors[k % colors.length];
        return {
          type: "bar", label: s.label,
          data: s.values.map(function (p) { return p ? p.v : null; }),
          _q: s.values,
          backgroundColor: qColors(s.values, c),
          hoverBackgroundColor: c,
          borderColor: t.surface,
          borderWidth: { top: k ? 2 : 0, bottom: 0, left: 0, right: 0 },
          borderSkipped: false,
          maxBarThickness: 24,
          stack: "s",
        };
      });
      var opts = baseOptions(t);
      opts.scales.x.stacked = true;
      opts.scales.y.stacked = true;
      opts.plugins.tooltip.callbacks = {
        label: function (ctx) {
          var p = ctx.dataset._q[ctx.dataIndex];
          return p ? qTooltipLine(ctx.dataset.label, p, true) : null;
        },
        footer: function (items) { return provenanceFooter(items.map(function (it) { return it.dataset._q[it.dataIndex]; })); },
      };
      renderLegend(legendId, data.series.map(function (s, k) { return { label: s.label, color: colors[k % colors.length] }; })
        .concat(hasProvisional(data.series.map(function (x) { return x.values; })) ? [HATCH_ITEM] : []));
      return new Chart(canvas, { data: { labels: data.labels, datasets: datasets }, options: opts });
    });
  }

  /* Point page: counter states over time, one chart per register (T1 and T2 counters differ by orders of
     magnitude); one line per meter (replacement = new line). data: {series:[{label, register, points:[[ms, value, "source"]]}]} */
  function stateLines(canvasId, dataId, legendId, reg) {
    var data = readJSON(dataId);
    var canvas = document.getElementById(canvasId);
    if (!data || !canvas || !window.Chart) return;
    var series = data.series.filter(function (s) { return !reg || s.register === reg; });
    register(function () {
      var t = tokens();
      var base = data.tenant_id ? tenantColor(data.tenant_id) : t.accent;
      var palette = [t.text2, base, t.muted, alpha(base, 0.55)];
      if (series.length === 1) palette = [base];
      var datasets = series.map(function (s, k) {
        var c = palette[k % palette.length];
        return {
          type: "line", label: s.label,
          data: s.points.map(function (p) { return { x: p[0], y: p[1] }; }),
          borderColor: c, backgroundColor: c, borderWidth: 2,
          pointRadius: s.points.length > 60 ? 2 : 4, pointHoverRadius: 5,
          pointBorderColor: t.surface, pointBorderWidth: s.points.length > 60 ? 0 : 2,
          tension: 0,
        };
      });
      var opts = baseOptions(t);
      opts.interaction = { mode: "nearest", intersect: false, axis: "x" };
      opts.scales.x = {
        type: "linear",
        grid: { display: false },
        border: { color: t.grid },
        ticks: { color: t.text2, maxTicksLimit: isNarrow() ? 4 : 8, callback: function (v) { return fmtMonth(v); } },
      };
      opts.scales.y.ticks.callback = function (v) { return fmt(v, 0); };
      opts.scales.y.beginAtZero = false;
      opts.scales.y.title.text = "stav počitadla";
      opts.plugins.tooltip.callbacks = {
        title: function (items) { return items.length ? fmtDate(items[0].parsed.x) : ""; },
        label: function (ctx) { return ctx.dataset.label + ": " + fmt(ctx.parsed.y, 0); },
      };
      renderLegend(legendId, series.map(function (s, k) { return { label: s.label, color: palette[k % palette.length], line: true }; }));
      return new Chart(canvas, { data: { datasets: datasets }, options: opts });
    });
  }

  // --- provenance popover for quality-marked values (tap on phones, Enter/Space on keyboard) --------
  var pop = null;
  function hidePop() { if (pop) { pop.remove(); pop = null; } }
  function showPop(el) {
    hidePop();
    var text = el.getAttribute("data-title") || el.getAttribute("title");
    if (!text) return;
    el.setAttribute("data-title", text);
    pop = document.createElement("div");
    pop.className = "q-popover";
    pop.setAttribute("role", "tooltip");
    text.split("\n").forEach(function (line, i) {
      var p = document.createElement(i === 0 ? "strong" : "div");
      p.textContent = line;
      pop.appendChild(p);
    });
    document.body.appendChild(pop);
    var r = el.getBoundingClientRect();
    var w = pop.offsetWidth;
    var left = Math.max(8, Math.min(window.scrollX + r.left, window.scrollX + document.documentElement.clientWidth - w - 8));
    pop.style.left = left + "px";
    pop.style.top = (window.scrollY + r.bottom + 6) + "px";
  }
  var QSEL = ".q-provisional[title], .q-provisional[data-title], [data-q-pop]";
  document.addEventListener("click", function (evt) {
    var el = evt.target.closest(QSEL);
    if (el) { evt.preventDefault(); if (pop && pop._for === el) { hidePop(); } else { showPop(el); if (pop) pop._for = el; } return; }
    if (pop && !evt.target.closest(".q-popover")) hidePop();
  });
  document.addEventListener("keydown", function (evt) {
    if (evt.key === "Escape") { hidePop(); return; }
    if (evt.key !== "Enter" && evt.key !== " ") return;
    var el = evt.target.closest && evt.target.closest(QSEL);
    if (el) { evt.preventDefault(); showPop(el); if (pop) pop._for = el; }
  });
  window.addEventListener("resize", hidePop);

  window.LKHCCharts = {
    monthlyStacked: monthlyStacked,
    monthlyBars: monthlyBars,
    registerBars: registerBars,
    stateLines: stateLines,
    rebuildAll: rebuildAll,
  };
})();
