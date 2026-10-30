/* Lightweight canvas charts (no external dependencies).
 * CandleChart: candles + EMA/Bollinger overlays, volume, RSI & MACD panes, trade markers,
 *              entry/SL/TP lines; pan (drag), zoom (wheel / pinch), crosshair (mouse / touch).
 * drawLine:    simple area/line chart for equity & drawdown.
 */
(function () {
  const C = {
    bg: "#0f1114", grid: "#1a1f25", text: "#7d8791", up: "#3ddc84", down: "#ff5f56",
    ema20: "#f5b942", ema50: "#5aa9ff", ema200: "#b48cff", bb: "rgba(230,233,236,.28)",
    cross: "rgba(230,233,236,.35)", sl: "#ff5f56", tp: "#3ddc84", entry: "#5aa9ff",
  };
  const font = (px) => `${px}px "JetBrains Mono", ui-monospace, Menlo, monospace`;

  function fmtPrice(v) {
    if (v == null || !isFinite(v)) return "–";
    const a = Math.abs(v);
    const d = a >= 1000 ? 2 : a >= 10 ? 3 : a >= 1 ? 4 : a >= 0.01 ? 5 : 8;
    return v.toLocaleString("de-DE", { minimumFractionDigits: d, maximumFractionDigits: d });
  }
  function fmtTime(t, tf) {
    const d = new Date(t * 1000);
    const o = { timeZone: "Europe/Berlin" };
    if (tf === "1d") return d.toLocaleDateString("de-DE", { ...o, day: "2-digit", month: "2-digit", year: "2-digit" });
    if (tf === "4h" || tf === "1h") return d.toLocaleString("de-DE", { ...o, day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" });
    return d.toLocaleTimeString("de-DE", { ...o, hour: "2-digit", minute: "2-digit" });
  }

  class CandleChart {
    constructor(el) {
      this.el = el;
      this.canvas = document.createElement("canvas");
      el.appendChild(this.canvas);
      this.ctx = this.canvas.getContext("2d");
      this.data = null;
      this.view = { end: 0, count: 120 };
      this.show = { ema: true, bb: true };
      this.cursor = null;
      this.pointers = new Map();
      this._bind();
      new ResizeObserver(() => this.draw()).observe(el);
    }
    setData(d, keepView) {
      const n = d.t.length;
      const first = !this.data || this.data.symbol !== d.symbol || this.data.tf !== d.tf;
      const wasAtEnd = !first && this.view.end >= this.data.t.length - 1;
      this.data = d;
      if (first || !keepView || wasAtEnd) this.view.end = n;
      if (first || !keepView) this.view.count = Math.min(n, this.el.clientWidth < 700 ? 70 : 130);
      this.view.count = this._clampCount(this.view.count);
      this.view.end = this._clampEnd(this.view.end);
      this.draw();
    }
    _bind() {
      const c = this.canvas;
      c.addEventListener("pointerdown", (e) => {
        c.setPointerCapture(e.pointerId);
        this.pointers.set(e.pointerId, { x: e.offsetX, y: e.offsetY, x0: e.offsetX, end0: this.view.end, count0: this.view.count });
        if (this.pointers.size === 2) {
          const [a, b] = [...this.pointers.values()];
          this.pinch0 = { d: Math.abs(a.x - b.x) || 1, count: this.view.count };
        }
        if (e.pointerType !== "mouse") { this.cursor = { x: e.offsetX, y: e.offsetY }; this.draw(); }
      });
      c.addEventListener("pointermove", (e) => {
        const p = this.pointers.get(e.pointerId);
        if (p) { p.x = e.offsetX; p.y = e.offsetY; }
        if (this.pointers.size === 2 && this.pinch0) {
          const [a, b] = [...this.pointers.values()];
          const d = Math.abs(a.x - b.x) || 1;
          this.view.count = this._clampCount(this.pinch0.count * this.pinch0.d / d);
          this.draw();
          return;
        }
        if (p && (e.pointerType === "mouse" ? e.buttons === 1 : true)) {
          const bw = this._plotW() / this.view.count;
          const shift = Math.round((e.offsetX - p.x0) / bw);
          if (Math.abs(e.offsetX - p.x0) > 6 || e.pointerType === "mouse") {
            this.view.end = this._clampEnd(p.end0 - shift);
          }
        }
        this.cursor = { x: e.offsetX, y: e.offsetY };
        this.draw();
      });
      const up = (e) => { this.pointers.delete(e.pointerId); if (this.pointers.size < 2) this.pinch0 = null; };
      c.addEventListener("pointerup", up);
      c.addEventListener("pointercancel", up);
      c.addEventListener("pointerleave", (e) => { if (e.pointerType === "mouse") { this.cursor = null; this.draw(); } });
      c.addEventListener("wheel", (e) => {
        e.preventDefault();
        this.view.count = this._clampCount(this.view.count * (e.deltaY > 0 ? 1.12 : 0.89));
        this.draw();
      }, { passive: false });
    }
    _clampCount(c) { const n = this.data ? this.data.t.length : 100; return Math.max(15, Math.min(n, Math.round(c))); }
    _clampEnd(e) { const n = this.data ? this.data.t.length : 0; return Math.max(Math.min(this.view.count, n), Math.min(n + 5, e)); }
    _plotW() { return this.el.clientWidth - 78; }

    draw() {
      const d = this.data, el = this.el, ctx = this.ctx;
      const W = el.clientWidth, H = el.clientHeight, dpr = window.devicePixelRatio || 1;
      if (!W || !H) return;
      if (this.canvas.width !== W * dpr || this.canvas.height !== H * dpr) {
        this.canvas.width = W * dpr; this.canvas.height = H * dpr;
      }
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      ctx.fillStyle = C.bg; ctx.fillRect(0, 0, W, H);
      if (!d || !d.t.length) { ctx.fillStyle = C.text; ctx.font = font(12); ctx.fillText("Keine Daten", 16, 24); return; }
      const axisW = 78, timeH = 20, plotW = W - axisW;
      const mainH = Math.round((H - timeH) * 0.62), rsiH = Math.round((H - timeH) * 0.18), macdH = H - timeH - mainH - rsiH;
      const panes = { main: [0, mainH], rsi: [mainH, rsiH], macd: [mainH + rsiH, macdH] };
      const n = d.t.length, end = Math.min(n, this.view.end), start = Math.max(0, end - this.view.count);
      const count = this.view.count, bw = plotW / count;
      const xOf = (i) => (i - (end - count)) * bw + bw / 2;

      // ---- main pane scale
      let lo = Infinity, hi = -Infinity;
      for (let i = start; i < end; i++) {
        lo = Math.min(lo, d.l[i]); hi = Math.max(hi, d.h[i]);
        if (this.show.bb && d.ind.bb_upper[i] != null) { hi = Math.max(hi, d.ind.bb_upper[i]); lo = Math.min(lo, d.ind.bb_lower[i]); }
      }
      (d.lines || []).forEach((l) => { if (l.price < hi * 1.15 && l.price > lo * 0.85) { hi = Math.max(hi, l.price); lo = Math.min(lo, l.price); } });
      const pad = (hi - lo) * 0.06 || hi * 0.01; lo -= pad; hi += pad;
      const volH = mainH * 0.16;
      const yP = (v) => (hi - v) / (hi - lo) * (mainH - 8) + 4;

      // grid + price axis
      ctx.font = font(10.5); ctx.textBaseline = "middle";
      const ticks = 6;
      for (let k = 0; k <= ticks; k++) {
        const v = lo + (hi - lo) * k / ticks, y = yP(v);
        ctx.strokeStyle = C.grid; ctx.lineWidth = 1; ctx.beginPath(); ctx.moveTo(0, y); ctx.lineTo(plotW, y); ctx.stroke();
        ctx.fillStyle = C.text; ctx.fillText(fmtPrice(v), plotW + 6, y);
      }
      // volume
      let vmax = 0; for (let i = start; i < end; i++) vmax = Math.max(vmax, d.v[i] || 0);
      for (let i = start; i < end; i++) {
        const h = vmax ? (d.v[i] / vmax) * volH : 0, x = xOf(i);
        ctx.fillStyle = d.c[i] >= d.o[i] ? "rgba(61,220,132,.18)" : "rgba(255,95,86,.18)";
        ctx.fillRect(x - bw * 0.35, mainH - h, Math.max(1, bw * 0.7), h);
      }
      // overlays
      const line = (arr, color, pane, yf, width = 1.3) => {
        ctx.strokeStyle = color; ctx.lineWidth = width; ctx.beginPath(); let pen = false;
        for (let i = start; i < end; i++) {
          const v = arr[i]; if (v == null) { pen = false; continue; }
          const x = xOf(i), y = yf(v);
          pen ? ctx.lineTo(x, y) : ctx.moveTo(x, y); pen = true;
        }
        ctx.stroke();
      };
      ctx.save(); ctx.beginPath(); ctx.rect(0, 0, plotW, mainH); ctx.clip();
      if (this.show.bb) { line(d.ind.bb_upper, C.bb, "main", yP, 1); line(d.ind.bb_lower, C.bb, "main", yP, 1); line(d.ind.bb_mid, "rgba(230,233,236,.14)", "main", yP, 1); }
      if (this.show.ema) { line(d.ind.ema20, C.ema20, "main", yP); line(d.ind.ema50, C.ema50, "main", yP); line(d.ind.ema200, C.ema200, "main", yP); }
      // candles
      for (let i = start; i < end; i++) {
        const x = xOf(i), up = d.c[i] >= d.o[i], col = up ? C.up : C.down;
        ctx.strokeStyle = col; ctx.fillStyle = col; ctx.lineWidth = 1;
        ctx.beginPath(); ctx.moveTo(Math.round(x) + .5, yP(d.h[i])); ctx.lineTo(Math.round(x) + .5, yP(d.l[i])); ctx.stroke();
        const y1 = yP(Math.max(d.o[i], d.c[i])), y2 = yP(Math.min(d.o[i], d.c[i]));
        const w = Math.max(1, bw * 0.68);
        ctx.fillRect(x - w / 2, y1, w, Math.max(1, y2 - y1));
      }
      // position lines
      (d.lines || []).forEach((l) => {
        const y = yP(l.price); if (y < 0 || y > mainH) return;
        ctx.strokeStyle = C[l.kind] || C.entry; ctx.setLineDash([5, 4]); ctx.lineWidth = 1;
        ctx.beginPath(); ctx.moveTo(0, y); ctx.lineTo(plotW, y); ctx.stroke(); ctx.setLineDash([]);
        ctx.fillStyle = C[l.kind] || C.entry; ctx.font = font(10); ctx.fillText(l.label, 6, y - 8);
      });
      // trade markers
      const tf = d.tf, step = { "1m": 60, "5m": 300, "15m": 900, "1h": 3600, "4h": 14400, "1d": 86400 }[tf];
      (d.markers || []).forEach((m) => {
        const idx = this._indexOf(Math.floor(m.time / step) * step); if (idx < start || idx >= end) return;
        const x = xOf(idx), y = yP(m.price), buy = (m.type === "entry") === (m.side === "long");
        ctx.fillStyle = m.type === "entry" ? (buy ? C.up : C.down) : "#e6e9ec";
        ctx.beginPath();
        if (buy) { ctx.moveTo(x, y + 4); ctx.lineTo(x - 6, y + 14); ctx.lineTo(x + 6, y + 14); }
        else { ctx.moveTo(x, y - 4); ctx.lineTo(x - 6, y - 14); ctx.lineTo(x + 6, y - 14); }
        ctx.fill();
      });
      ctx.restore();

      // ---- RSI pane
      const [ry, rh] = panes.rsi, yR = (v) => ry + 6 + (100 - v) / 100 * (rh - 12);
      ctx.strokeStyle = C.grid; ctx.beginPath(); ctx.moveTo(0, ry); ctx.lineTo(W, ry); ctx.stroke();
      [30, 70].forEach((lv) => { ctx.setLineDash([3, 4]); ctx.strokeStyle = "#2a3038"; ctx.beginPath(); ctx.moveTo(0, yR(lv)); ctx.lineTo(plotW, yR(lv)); ctx.stroke(); ctx.setLineDash([]); ctx.fillStyle = C.text; ctx.font = font(10); ctx.fillText(lv, plotW + 6, yR(lv)); });
      ctx.save(); ctx.beginPath(); ctx.rect(0, ry, plotW, rh); ctx.clip(); line(d.ind.rsi, "#5aa9ff", "rsi", yR); ctx.restore();
      ctx.fillStyle = C.text; ctx.fillText("RSI 14", 6, ry + 12);

      // ---- MACD pane
      const [my, mh] = panes.macd;
      let mm = 0; for (let i = start; i < end; i++) ["macd", "macd_signal", "macd_hist"].forEach((k) => { const v = d.ind[k][i]; if (v != null) mm = Math.max(mm, Math.abs(v)); });
      mm = mm || 1; const yM = (v) => my + mh / 2 - v / mm * (mh / 2 - 8);
      ctx.strokeStyle = C.grid; ctx.beginPath(); ctx.moveTo(0, my); ctx.lineTo(W, my); ctx.stroke();
      ctx.save(); ctx.beginPath(); ctx.rect(0, my, plotW, mh); ctx.clip();
      for (let i = start; i < end; i++) { const v = d.ind.macd_hist[i]; if (v == null) continue; ctx.fillStyle = v >= 0 ? "rgba(61,220,132,.45)" : "rgba(255,95,86,.45)"; const y0 = yM(0), y1 = yM(v); ctx.fillRect(xOf(i) - bw * 0.3, Math.min(y0, y1), Math.max(1, bw * 0.6), Math.abs(y1 - y0)); }
      line(d.ind.macd, "#f5b942", "macd", yM); line(d.ind.macd_signal, "#b48cff", "macd", yM);
      ctx.restore(); ctx.fillStyle = C.text; ctx.fillText("MACD 12/26/9", 6, my + 12);

      // ---- time axis
      ctx.strokeStyle = C.grid; ctx.beginPath(); ctx.moveTo(0, H - timeH); ctx.lineTo(W, H - timeH); ctx.stroke();
      const every = Math.max(1, Math.round(count / Math.max(2, Math.floor(plotW / 110))));
      ctx.fillStyle = C.text; ctx.font = font(10);
      for (let i = start; i < end; i++) if ((i % every) === 0) ctx.fillText(fmtTime(d.t[i], tf), xOf(i) - 26, H - timeH / 2);

      // ---- crosshair
      if (this.cursor && this.cursor.x < plotW) {
        const i = Math.max(start, Math.min(end - 1, Math.round((this.cursor.x - bw / 2) / bw + (end - count))));
        const x = xOf(i);
        ctx.strokeStyle = C.cross; ctx.setLineDash([3, 3]);
        ctx.beginPath(); ctx.moveTo(x, 0); ctx.lineTo(x, H - timeH); ctx.stroke();
        if (this.cursor.y < mainH) { ctx.beginPath(); ctx.moveTo(0, this.cursor.y); ctx.lineTo(plotW, this.cursor.y); ctx.stroke();
          const pv = hi - (this.cursor.y - 4) / (mainH - 8) * (hi - lo);
          ctx.setLineDash([]); ctx.fillStyle = "#2a3038"; ctx.fillRect(plotW, this.cursor.y - 9, axisW, 18); ctx.fillStyle = "#e6e9ec"; ctx.fillText(fmtPrice(pv), plotW + 6, this.cursor.y); }
        ctx.setLineDash([]);
        const info = `${fmtTime(d.t[i], tf === "1d" ? "1h" : tf)}  O ${fmtPrice(d.o[i])}  H ${fmtPrice(d.h[i])}  L ${fmtPrice(d.l[i])}  C ${fmtPrice(d.c[i])}  RSI ${d.ind.rsi[i] != null ? d.ind.rsi[i].toFixed(1) : "–"}`;
        ctx.font = font(11); const tw = ctx.measureText(info).width;
        ctx.fillStyle = "rgba(15,17,20,.88)"; ctx.fillRect(4, 4, Math.min(tw + 12, plotW - 8), 20);
        ctx.fillStyle = "#e6e9ec"; ctx.fillText(info, 10, 14);
      }
    }
    _indexOf(t) {
      const a = this.data.t; let lo = 0, hi = a.length - 1;
      if (t < a[0]) return -1;
      while (lo < hi) { const m = (lo + hi + 1) >> 1; if (a[m] <= t) lo = m; else hi = m - 1; }
      return lo;
    }
  }

  function drawLine(canvas, pts, opts = {}) {
    const W = canvas.clientWidth, H = canvas.clientHeight, dpr = window.devicePixelRatio || 1;
    if (!W || !H) return;
    canvas.width = W * dpr; canvas.height = H * dpr;
    const ctx = canvas.getContext("2d"); ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, W, H);
    const axisW = 74, pw = W - axisW, ph = H - 18;
    if (!pts || pts.length < 2) { ctx.fillStyle = C.text; ctx.font = font(12); ctx.fillText(opts.empty || "Noch keine Daten", 8, 20); return; }
    let lo = Infinity, hi = -Infinity;
    pts.forEach((p) => { lo = Math.min(lo, p.y); hi = Math.max(hi, p.y); });
    if (opts.base != null) { lo = Math.min(lo, opts.base); hi = Math.max(hi, opts.base); }
    const pad = (hi - lo) * 0.08 || Math.abs(hi) * 0.01 || 1; lo -= pad; hi += pad;
    const t0 = pts[0].x, t1 = pts[pts.length - 1].x;
    const X = (x) => (x - t0) / ((t1 - t0) || 1) * pw, Y = (y) => (hi - y) / (hi - lo) * (ph - 8) + 4;
    ctx.font = font(10.5); ctx.textBaseline = "middle";
    for (let k = 0; k <= 4; k++) {
      const v = lo + (hi - lo) * k / 4, y = Y(v);
      ctx.strokeStyle = C.grid; ctx.beginPath(); ctx.moveTo(0, y); ctx.lineTo(pw, y); ctx.stroke();
      ctx.fillStyle = C.text; ctx.fillText(opts.fmt ? opts.fmt(v) : v.toFixed(2), pw + 6, y);
    }
    if (opts.base != null) { ctx.setLineDash([4, 4]); ctx.strokeStyle = "#3a414a"; ctx.beginPath(); ctx.moveTo(0, Y(opts.base)); ctx.lineTo(pw, Y(opts.base)); ctx.stroke(); ctx.setLineDash([]); }
    const last = pts[pts.length - 1].y;
    const color = opts.color || (opts.base != null ? (last >= opts.base ? C.up : C.down) : C.up);
    const g = ctx.createLinearGradient(0, 0, 0, ph);
    g.addColorStop(0, color + "40"); g.addColorStop(1, color + "00");
    ctx.beginPath(); pts.forEach((p, i) => (i ? ctx.lineTo(X(p.x), Y(p.y)) : ctx.moveTo(X(p.x), Y(p.y))));
    const baseY = opts.fillTo != null ? Y(opts.fillTo) : ph;
    ctx.lineTo(X(t1), baseY); ctx.lineTo(X(t0), baseY); ctx.closePath(); ctx.fillStyle = g; ctx.fill();
    ctx.beginPath(); pts.forEach((p, i) => (i ? ctx.lineTo(X(p.x), Y(p.y)) : ctx.moveTo(X(p.x), Y(p.y))));
    ctx.strokeStyle = color; ctx.lineWidth = 1.6; ctx.stroke();
    ctx.fillStyle = color; ctx.beginPath(); ctx.arc(X(t1), Y(last), 3.5, 0, Math.PI * 2); ctx.fill();
    ctx.fillStyle = C.text; ctx.font = font(10);
    const d0 = new Date(t0 * 1000), d1 = new Date(t1 * 1000);
    const f = (d) => d.toLocaleString("de-DE", { timeZone: "Europe/Berlin", day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" });
    ctx.fillText(f(d0), 2, H - 8); const s1 = f(d1); ctx.fillText(s1, pw - ctx.measureText(s1).width, H - 8);
  }

  window.Charts = { CandleChart, drawLine, fmtPrice };
})();
