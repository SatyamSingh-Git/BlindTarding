/* Interactive candlestick chart.
 *
 * Written directly against canvas rather than pulling a charting library: the
 * page has to work offline and on a blocked network, and the overlays here
 * (UT stop, ORB levels, data gaps) are specific enough that configuring someone
 * else's chart would be more work than drawing it.
 *
 * The whole bar array lives client-side and the viewport is pure local state,
 * so zoom and pan never touch the network. That is the difference between a
 * chart that feels like a tool and one that feels like a report.
 */
(function (global) {
  "use strict";

  const C = {
    up: "#26a65b", down: "#e5484d", hma: "#4c8dff",
    stop: "rgba(139,152,168,.85)", orbHi: "rgba(38,166,91,.7)",
    orbLo: "rgba(229,72,77,.7)", grid: "rgba(36,48,64,.55)",
    axis: "#5b6775", text: "#e6edf3", dim: "#8b98a8",
    gap: "rgba(227,160,8,.55)", cross: "rgba(180,195,210,.55)",
    measureUp: "rgba(38,166,91,.16)", measureDown: "rgba(229,72,77,.16)",
  };
  const PAD = { l: 8, r: 74, t: 10, b: 30 };
  const MIN_BARS = 20, MAX_BARS = 2000;

  function fmt(v, d) {
    if (v === null || v === undefined || isNaN(v)) return "—";
    return Number(v).toLocaleString("en-IN",
      { minimumFractionDigits: d === undefined ? 2 : d,
        maximumFractionDigits: d === undefined ? 2 : d });
  }
  const clockOf = iso => iso.slice(11, 16);
  const dayOf = iso => iso.slice(0, 10);

  function Chart(canvas, opts) {
    this.canvas = canvas;
    this.ctx = canvas.getContext("2d");
    this.bars = [];
    this.gaps = new Set();
    this.view = { end: 0, count: 180 };   // end is exclusive
    // null means fit-to-data. Once you drag vertically or scale the price
    // axis it becomes a locked window, because auto-fit would silently undo
    // the adjustment on the next redraw.
    this.priceRange = null;
    this.cursor = null;                   // index under the pointer
    this.drag = null;
    this.measure = null;                  // {a, b} bar indices
    this.measureMode = false;
    this.show = { hma: true, stop: true, orb: true, signals: true, gaps: true };
    this.onHover = (opts && opts.onHover) || function () {};
    this.onMeasure = (opts && opts.onMeasure) || function () {};
    this._bind();
  }

  Chart.prototype.setData = function (bars, gaps) {
    const keepTail = this.bars.length === 0 ||
      this.view.end >= this.bars.length - 1;
    this.bars = bars || [];
    this.gaps = new Set(gaps || []);
    if (keepTail || this.view.end > this.bars.length) {
      this.view.end = this.bars.length;
    }
    this.view.count = Math.min(this.view.count, Math.max(MIN_BARS, this.bars.length));
    this.draw();
  };

  Chart.prototype.toggle = function (key, on) {
    this.show[key] = on;
    this.draw();
  };

  Chart.prototype.setMeasureMode = function (on) {
    this.measureMode = on;
    if (!on) { this.measure = null; this.onMeasure(null); }
    this.canvas.style.cursor = on ? "crosshair" : "default";
    this.draw();
  };

  Chart.prototype.resetView = function () {
    this.view.end = this.bars.length;
    this.view.count = Math.min(180, Math.max(MIN_BARS, this.bars.length));
    this.priceRange = null;
    this.measure = null; this.onMeasure(null);
    this.draw();
  };

  Chart.prototype.autoPrice = function () {
    this.priceRange = null;
    this.draw();
  };

  Chart.prototype.scalePrice = function (factor, anchorPrice) {
    const m = this._geom();
    const lo = this.priceRange ? this.priceRange.lo : m.lo;
    const hi = this.priceRange ? this.priceRange.hi : m.hi;
    const mid = anchorPrice === undefined ? (lo + hi) / 2 : anchorPrice;
    const span = (hi - lo) * factor;
    const frac = (hi - lo) ? (mid - lo) / (hi - lo) : 0.5;
    this.priceRange = { lo: mid - span * frac, hi: mid + span * (1 - frac) };
    this.draw();
  };

  Chart.prototype.zoomBy = function (factor, anchorIdx) {
    const v = this.view, before = v.count;
    v.count = Math.max(MIN_BARS, Math.min(MAX_BARS, Math.round(v.count * factor)));
    if (v.count === before) return;
    if (anchorIdx !== undefined && anchorIdx !== null) {
      // Keep the bar under the pointer where it is, which is what makes
      // wheel-zoom feel like a map rather than a slider.
      const frac = (anchorIdx - (v.end - before)) / before;
      v.end = Math.round(anchorIdx + (1 - frac) * v.count);
    }
    this._clamp();
    this.draw();
  };

  Chart.prototype._clamp = function () {
    const v = this.view, n = this.bars.length;
    v.count = Math.max(MIN_BARS, Math.min(v.count, Math.max(MIN_BARS, n)));
    v.end = Math.max(Math.min(v.count, n), Math.min(v.end, n));
  };

  Chart.prototype._geom = function () {
    const W = this.canvas.clientWidth, H = this.canvas.clientHeight;
    const v = this.view, n = this.bars.length;
    const from = Math.max(0, v.end - v.count), to = Math.min(n, v.end);
    const slice = this.bars.slice(from, to);
    const plotW = W - PAD.l - PAD.r, plotH = H - PAD.t - PAD.b;
    const step = plotW / Math.max(1, slice.length);

    let lo = Infinity, hi = -Infinity;
    for (const b of slice) {
      if (b.h > hi) hi = b.h;
      if (b.l < lo) lo = b.l;
      if (this.show.hma && b.hma != null) { hi = Math.max(hi, b.hma); lo = Math.min(lo, b.hma); }
      if (this.show.stop && b.stop != null) { hi = Math.max(hi, b.stop); lo = Math.min(lo, b.stop); }
    }
    if (!isFinite(lo)) { lo = 0; hi = 1; }
    const pad = (hi - lo) * 0.07 || 1; lo -= pad; hi += pad;
    if (this.priceRange) { lo = this.priceRange.lo; hi = this.priceRange.hi; }

    return {
      W, H, from, to, slice, step, plotW, plotH, lo, hi,
      x: i => PAD.l + (i - from + 0.5) * step,
      y: p => PAD.t + (hi - p) / (hi - lo) * plotH,
      idxAt: px => Math.round(from + (px - PAD.l) / step - 0.5),
      priceAt: py => hi - (py - PAD.t) / plotH * (hi - lo),
      onPriceAxis: px => px > W - PAD.r,
      onTimeAxis: py => py > H - PAD.b,
    };
  };

  Chart.prototype.draw = function () {
    const cv = this.canvas, g = this.ctx;
    const dpr = global.devicePixelRatio || 1;
    const W = cv.clientWidth, H = cv.clientHeight;
    if (!W || !H) return;
    cv.width = W * dpr; cv.height = H * dpr;
    g.setTransform(dpr, 0, 0, dpr, 0, 0);
    g.clearRect(0, 0, W, H);
    if (!this.bars.length) {
      g.fillStyle = C.dim; g.font = "13px system-ui"; g.textAlign = "center";
      g.fillText("no data", W / 2, H / 2); return;
    }

    const m = this._geom();
    this._grid(g, m);
    if (this.show.gaps) this._gaps(g, m);
    if (this.show.orb) { this._line(g, m, "orb_high", C.orbHi); this._line(g, m, "orb_low", C.orbLo); }
    if (this.show.stop) this._line(g, m, "stop", C.stop, [4, 3]);
    this._candles(g, m);
    if (this.show.hma) this._line(g, m, "hma", C.hma, null, 1.6);
    if (this.show.signals) this._markers(g, m);
    this._lastPrice(g, m);
    if (this.measure) this._measure(g, m);
    if (this.cursor !== null) this._crosshair(g, m);
  };

  Chart.prototype._grid = function (g, m) {
    g.font = "11px ui-monospace,Consolas,monospace";
    g.textBaseline = "middle"; g.lineWidth = 1;
    for (let k = 0; k <= 5; k++) {
      const p = m.lo + (m.hi - m.lo) * k / 5, y = m.y(p);
      g.strokeStyle = C.grid;
      g.beginPath(); g.moveTo(PAD.l, y); g.lineTo(m.W - PAD.r, y); g.stroke();
      g.fillStyle = C.axis; g.textAlign = "left";
      g.fillText(fmt(p, 0), m.W - PAD.r + 7, y);
    }
    // Session dividers, plus a time label roughly every 90px.
    g.textAlign = "center"; g.textBaseline = "top";
    let lastDay = null, lastLabelX = -999;
    m.slice.forEach((b, k) => {
      const i = m.from + k, x = m.x(i), day = dayOf(b.t);
      if (lastDay && day !== lastDay) {
        g.strokeStyle = "rgba(91,103,117,.45)"; g.setLineDash([3, 3]);
        g.beginPath(); g.moveTo(x, PAD.t); g.lineTo(x, m.H - PAD.b); g.stroke();
        g.setLineDash([]);
        if (x - lastLabelX > 46) {
          g.fillStyle = C.dim;
          g.fillText(b.t.slice(8, 10) + " " +
            new Date(b.t).toLocaleDateString("en-IN", { month: "short" }),
            x, m.H - PAD.b + 8);
          lastLabelX = x;
        }
      } else if (x - lastLabelX > 92) {
        g.fillStyle = C.axis;
        g.fillText(clockOf(b.t), x, m.H - PAD.b + 8);
        lastLabelX = x;
      }
      lastDay = day;
    });
  };

  Chart.prototype._gaps = function (g, m) {
    if (!this.gaps.size) return;
    // A gap sits between two bars we do have, so mark the boundary rather than
    // inventing a slot for it.
    g.strokeStyle = C.gap; g.lineWidth = 2;
    m.slice.forEach((b, k) => {
      const i = m.from + k;
      if (k === 0) return;
      const prev = m.slice[k - 1];
      let missing = false;
      this.gaps.forEach(gp => { if (gp > prev.t && gp < b.t) missing = true; });
      if (!missing) return;
      const x = (m.x(i) + m.x(i - 1)) / 2;
      g.setLineDash([2, 3]);
      g.beginPath(); g.moveTo(x, PAD.t); g.lineTo(x, m.H - PAD.b); g.stroke();
      g.setLineDash([]);
    });
  };

  Chart.prototype._line = function (g, m, key, colour, dash, width) {
    g.strokeStyle = colour; g.lineWidth = width || 1.3;
    g.setLineDash(dash || []);
    g.beginPath();
    let open = false;
    m.slice.forEach((b, k) => {
      const v = b[key], i = m.from + k;
      if (v === null || v === undefined) { open = false; return; }
      if (!open) { g.moveTo(m.x(i), m.y(v)); open = true; }
      else g.lineTo(m.x(i), m.y(v));
    });
    g.stroke(); g.setLineDash([]);
  };

  Chart.prototype._candles = function (g, m) {
    const bw = Math.max(1, Math.min(14, m.step * 0.66));
    m.slice.forEach((b, k) => {
      const i = m.from + k, x = m.x(i);
      const up = b.c >= b.o, col = up ? C.up : C.down;
      g.strokeStyle = col; g.fillStyle = col; g.lineWidth = 1;
      g.beginPath(); g.moveTo(x, m.y(b.h)); g.lineTo(x, m.y(b.l)); g.stroke();
      const y1 = m.y(b.o), y2 = m.y(b.c);
      g.fillRect(x - bw / 2, Math.min(y1, y2), bw, Math.max(1, Math.abs(y2 - y1)));
    });
  };

  Chart.prototype._markers = function (g, m) {
    m.slice.forEach((b, k) => {
      if (!b.buy && !b.sell) return;
      const i = m.from + k, x = m.x(i);
      const up = b.buy, y = up ? m.y(b.l) + 12 : m.y(b.h) - 12;
      g.fillStyle = up ? C.up : C.down;
      g.beginPath();
      if (up) { g.moveTo(x, y - 8); g.lineTo(x - 6, y + 2); g.lineTo(x + 6, y + 2); }
      else { g.moveTo(x, y + 8); g.lineTo(x - 6, y - 2); g.lineTo(x + 6, y - 2); }
      g.closePath(); g.fill();
    });
  };

  Chart.prototype._lastPrice = function (g, m) {
    const b = this.bars[this.bars.length - 1];
    if (!b) return;
    const inView = m.to === this.bars.length;
    const y = m.y(inView ? b.c : m.slice[m.slice.length - 1].c);
    const price = inView ? b.c : m.slice[m.slice.length - 1].c;
    if (y < PAD.t || y > m.H - PAD.b) return;
    g.fillStyle = price >= b.o ? C.up : C.down;
    g.fillRect(m.W - PAD.r + 2, y - 9, PAD.r - 5, 18);
    g.fillStyle = "#08111a"; g.textAlign = "left"; g.textBaseline = "middle";
    g.font = "600 11px ui-monospace,Consolas,monospace";
    g.fillText(fmt(price), m.W - PAD.r + 7, y);
  };

  Chart.prototype._crosshair = function (g, m) {
    const i = this.cursor;
    if (i < m.from || i >= m.to) return;
    const b = this.bars[i], x = m.x(i);
    g.strokeStyle = C.cross; g.lineWidth = 1; g.setLineDash([3, 3]);
    g.beginPath(); g.moveTo(x, PAD.t); g.lineTo(x, m.H - PAD.b); g.stroke();
    if (this.pointerY != null) {
      g.beginPath(); g.moveTo(PAD.l, this.pointerY);
      g.lineTo(m.W - PAD.r, this.pointerY); g.stroke();
    }
    g.setLineDash([]);
    if (this.pointerY != null) {
      const p = m.priceAt(this.pointerY);
      g.fillStyle = "#2a3542";
      g.fillRect(m.W - PAD.r + 2, this.pointerY - 9, PAD.r - 5, 18);
      g.fillStyle = C.text; g.textAlign = "left"; g.textBaseline = "middle";
      g.font = "11px ui-monospace,Consolas,monospace";
      g.fillText(fmt(p), m.W - PAD.r + 7, this.pointerY);
    }
    // time tag
    g.fillStyle = "#2a3542";
    const label = b.t.slice(8, 10) + " " + clockOf(b.t);
    g.font = "11px ui-monospace,Consolas,monospace";
    const w = g.measureText(label).width + 12;
    g.fillRect(Math.min(Math.max(x - w / 2, PAD.l), m.W - PAD.r - w),
               m.H - PAD.b + 2, w, 17);
    g.fillStyle = C.text; g.textAlign = "center"; g.textBaseline = "top";
    g.fillText(label, Math.min(Math.max(x, PAD.l + w / 2), m.W - PAD.r - w / 2),
               m.H - PAD.b + 5);
  };

  Chart.prototype._measure = function (g, m) {
    const a = Math.min(this.measure.a, this.measure.b);
    const bIdx = Math.max(this.measure.a, this.measure.b);
    if (a < 0 || bIdx >= this.bars.length) return;
    const p1 = this.measure.p1, p2 = this.measure.p2;
    const x1 = m.x(a), x2 = m.x(bIdx), y1 = m.y(p1), y2 = m.y(p2);
    const rising = p2 >= p1;

    g.fillStyle = rising ? C.measureUp : C.measureDown;
    g.fillRect(Math.min(x1, x2), Math.min(y1, y2),
               Math.abs(x2 - x1) || 1, Math.abs(y2 - y1) || 1);
    g.strokeStyle = rising ? C.up : C.down; g.lineWidth = 1.2;
    g.setLineDash([4, 3]);
    g.strokeRect(Math.min(x1, x2), Math.min(y1, y2),
                 Math.abs(x2 - x1) || 1, Math.abs(y2 - y1) || 1);
    g.setLineDash([]);

    const dPts = p2 - p1, dPct = p1 ? (dPts / p1) * 100 : 0;
    const nBars = bIdx - a;
    const mins = (new Date(this.bars[bIdx].t) - new Date(this.bars[a].t)) / 60000;
    const dur = mins >= 1440 ? (mins / 1440).toFixed(1) + "d"
      : mins >= 60 ? Math.floor(mins / 60) + "h " + Math.round(mins % 60) + "m"
      : Math.round(mins) + "m";
    const text = (dPts >= 0 ? "+" : "") + fmt(dPts) + "  (" +
      (dPct >= 0 ? "+" : "") + dPct.toFixed(2) + "%)";
    const sub = nBars + " bars · " + dur;

    g.font = "600 12px ui-monospace,Consolas,monospace";
    const w = Math.max(g.measureText(text).width, g.measureText(sub).width) + 16;
    let bx = (x1 + x2) / 2 - w / 2;
    bx = Math.max(PAD.l, Math.min(bx, m.W - PAD.r - w));
    const by = Math.min(y1, y2) - 42 < PAD.t ? Math.max(y1, y2) + 8
                                            : Math.min(y1, y2) - 42;
    g.fillStyle = rising ? "rgba(38,166,91,.92)" : "rgba(229,72,77,.92)";
    g.fillRect(bx, by, w, 36);
    g.fillStyle = "#08111a"; g.textAlign = "center"; g.textBaseline = "top";
    g.fillText(text, bx + w / 2, by + 4);
    g.font = "11px ui-monospace,Consolas,monospace";
    g.fillText(sub, bx + w / 2, by + 20);
  };

  Chart.prototype._bind = function () {
    const cv = this.canvas, self = this;

    function zone(m, pos) {
      if (m.onPriceAxis(pos.x)) return "price";
      if (m.onTimeAxis(pos.y)) return "time";
      return "plot";
    }

    cv.addEventListener("wheel", e => {
      e.preventDefault();
      const m = self._geom(), pos = self._px(e), where = zone(m, pos);
      const factor = e.deltaY > 0 ? 1.18 : 1 / 1.18;
      // Over the price axis the wheel scales price only; elsewhere it scales
      // time. Matches how every charting tool behaves, and means you can
      // stretch one axis without disturbing the other.
      if (where === "price") self.scalePrice(factor, m.priceAt(pos.y));
      else self.zoomBy(factor, m.idxAt(pos.x));
    }, { passive: false });

    cv.addEventListener("mousedown", e => {
      const m = self._geom(), pos = self._px(e), where = zone(m, pos);
      const i = m.idxAt(pos.x);

      if (where === "price") {
        self.drag = { kind: "scaleY", y: pos.y,
                      lo: (self.priceRange || m).lo, hi: (self.priceRange || m).hi,
                      anchor: m.priceAt(pos.y), plotH: m.plotH };
        cv.style.cursor = "ns-resize";
        return;
      }
      if (where === "time") {
        self.drag = { kind: "scaleX", x: pos.x, count: self.view.count,
                      end: self.view.end };
        cv.style.cursor = "ew-resize";
        return;
      }
      if (self.measureMode || e.shiftKey) {
        self.measure = { a: i, b: i, p1: m.priceAt(pos.y), p2: m.priceAt(pos.y) };
        self.drag = { kind: "measure" };
        return;
      }
      // Plot drag pans both axes. Panning vertically locks the price window,
      // otherwise the next auto-fit would throw the movement away.
      self.drag = { kind: "pan", x: pos.x, y: pos.y, end: self.view.end,
                    lo: m.lo, hi: m.hi, plotH: m.plotH };
      cv.style.cursor = "grabbing";
    });

    global.addEventListener("mouseup", () => {
      if (self.drag) cv.style.cursor = self.measureMode ? "crosshair" : "default";
      self.drag = null;
    });

    cv.addEventListener("mousemove", e => {
      const m = self._geom(), pos = self._px(e);
      self.pointerY = pos.y;
      const i = m.idxAt(pos.x);
      const d = self.drag;

      if (d && d.kind === "pan") {
        self.view.end = d.end + Math.round((d.x - pos.x) / m.step);
        self._clamp();
        const dy = pos.y - d.y;
        if (Math.abs(dy) > 1) {
          const shift = dy / d.plotH * (d.hi - d.lo);
          self.priceRange = { lo: d.lo + shift, hi: d.hi + shift };
        }
        self.draw(); return;
      }
      if (d && d.kind === "scaleY") {
        // Drag down to compress, up to expand — the price axis stretches.
        const factor = Math.max(0.05, Math.min(20,
          1 + (pos.y - d.y) / Math.max(60, d.plotH * 0.5)));
        const span = (d.hi - d.lo) * factor;
        const frac = (d.hi - d.lo) ? (d.anchor - d.lo) / (d.hi - d.lo) : 0.5;
        self.priceRange = { lo: d.anchor - span * frac,
                            hi: d.anchor + span * (1 - frac) };
        self.draw(); return;
      }
      if (d && d.kind === "scaleX") {
        const factor = Math.max(0.05, Math.min(20,
          1 + (d.x - pos.x) / Math.max(80, m.plotW * 0.5)));
        self.view.count = Math.max(MIN_BARS,
          Math.min(MAX_BARS, Math.round(d.count * factor)));
        self.view.end = d.end;
        self._clamp(); self.draw(); return;
      }
      if (d && d.kind === "measure") {
        self.measure.b = Math.max(0, Math.min(self.bars.length - 1, i));
        self.measure.p2 = m.priceAt(pos.y);
        self.onMeasure(self._measureInfo());
        self.draw(); return;
      }

      const where = zone(m, pos);
      cv.style.cursor = where === "price" ? "ns-resize"
        : where === "time" ? "ew-resize"
        : self.measureMode ? "crosshair" : "grab";
      self.cursor = (i >= m.from && i < m.to) ? i : null;
      self.onHover(self.cursor === null ? null : self.bars[self.cursor]);
      self.draw();
    });

    cv.addEventListener("mouseleave", () => {
      self.cursor = null; self.pointerY = null;
      self.onHover(null); self.draw();
    });

    cv.addEventListener("dblclick", e => {
      const m = self._geom(), pos = self._px(e), where = zone(m, pos);
      // Double-click an axis to release just that axis; anywhere else resets both.
      if (where === "price") self.autoPrice();
      else if (where === "time") { self.view.count = 180; self._clamp(); self.draw(); }
      else self.resetView();
    });
  };

  Chart.prototype._measureInfo = function () {
    if (!this.measure) return null;
    const a = Math.min(this.measure.a, this.measure.b);
    const b = Math.max(this.measure.a, this.measure.b);
    const dPts = this.measure.p2 - this.measure.p1;
    return {
      points: dPts,
      percent: this.measure.p1 ? (dPts / this.measure.p1) * 100 : 0,
      bars: b - a,
      from: this.bars[a] ? this.bars[a].t : null,
      to: this.bars[b] ? this.bars[b].t : null,
    };
  };

  Chart.prototype._px = function (e) {
    const r = this.canvas.getBoundingClientRect();
    return { x: e.clientX - r.left, y: e.clientY - r.top };
  };

  global.OrbotChart = Chart;
})(window);
