/* Dashboard logic: polling the API, rendering tabs. No framework, no build step. */
(function () {
  const $ = (s) => document.querySelector(s);
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const eur = (v, d = 2) => v == null ? "–" : v.toLocaleString("de-DE", { style: "currency", currency: "EUR", minimumFractionDigits: d, maximumFractionDigits: d });
  const num = (v, d = 2) => v == null || v === "inf" ? (v === "inf" ? "∞" : "–") : Number(v).toLocaleString("de-DE", { minimumFractionDigits: d, maximumFractionDigits: d });
  const pct = (v, d = 2, sign = true) => v == null ? "–" : (sign && v > 0 ? "+" : "") + num(v, d) + " %";
  const cls = (v) => v > 0 ? "pos" : v < 0 ? "neg" : "";
  const price = (v) => Charts.fmtPrice(v);
  const compact = (v) => v == null ? "–" : Intl.NumberFormat("de-DE", { notation: "compact", maximumFractionDigits: 1 }).format(v);
  const dt = (t) => t ? new Date(t * 1000).toLocaleString("de-DE", { timeZone: "Europe/Berlin", day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit" }) : "–";
  const hm = (t) => t ? new Date(t * 1000).toLocaleTimeString("de-DE", { timeZone: "Europe/Berlin", hour: "2-digit", minute: "2-digit", second: "2-digit" }) : "–";
  const ago = (t) => { if (!t) return "nie"; const s = Date.now() / 1000 - t; if (s < 60) return `vor ${Math.max(0, s | 0)} s`; if (s < 3600) return `vor ${(s / 60) | 0} min`; if (s < 86400) return `vor ${(s / 3600).toFixed(1)} h`; return `vor ${(s / 86400).toFixed(1)} T`; };
  const STATUS_DOT = { ok: "green", working: "green", idle: "yellow", warning: "yellow", halted: "yellow", error: "red", never: "" };

  async function api(path, opts = {}) {
    const r = await fetch(path, { credentials: "same-origin", headers: { "Content-Type": "application/json" }, ...opts });
    if (r.status === 401) { location.reload(); throw new Error("auth"); }
    if (!r.ok) { let m = r.statusText; try { m = (await r.json()).detail || m; } catch (e) {} throw new Error(m); }
    return r.json();
  }
  function toast(msg) { const t = $("#toast"); t.textContent = msg; t.classList.remove("hidden"); clearTimeout(toast._t); toast._t = setTimeout(() => t.classList.add("hidden"), 3500); }
  function modal(html) { $("#modal-body").innerHTML = html; $("#modal").classList.remove("hidden"); }
  $("#modal-x").onclick = () => $("#modal").classList.add("hidden");
  $("#modal").onclick = (e) => { if (e.target.id === "modal") $("#modal").classList.add("hidden"); };

  // ------------------------------------------------------------- tabs
  let active = localStorageGet("tab") || "overview";
  function localStorageGet(k) { try { return localStorage.getItem(k); } catch (e) { return null; } }
  function localStorageSet(k, v) { try { localStorage.setItem(k, v); } catch (e) {} }
  function showTab(name) {
    active = name; localStorageSet("tab", name);
    document.querySelectorAll(".tabs button").forEach((b) => b.classList.toggle("active", b.dataset.tab === name));
    document.querySelectorAll(".tab").forEach((t) => t.classList.toggle("active", t.id === "tab-" + name));
    refresh();
  }
  document.querySelectorAll(".tabs button").forEach((b) => (b.onclick = () => showTab(b.dataset.tab)));

  // ------------------------------------------------------------- header
  let overview = null;
  async function loadOverview() {
    overview = await api("/api/overview");
    $("#synthetic-banner").classList.toggle("hidden", !overview.synthetic);
    $("#brand-mode").textContent = overview.synthetic ? "TEST · SYNTHETISCH" : "PAPER";
    const h = overview.health, pill = $("#health-pill");
    pill.querySelector(".dot").className = "dot " + h;
    pill.querySelector("span").textContent = { green: "System OK", yellow: "Eingeschränkt", red: "Störung" }[h] || h;
    const k = $("#btn-kill"); k.classList.toggle("on", !!overview.kill_switch.active);
    k.textContent = overview.kill_switch.active ? "Kill Switch AKTIV" : "Kill Switch";
    return overview;
  }
  $("#btn-kill").onclick = async () => {
    const on = overview && overview.kill_switch.active;
    if (!confirm(on ? "Kill Switch aufheben und Handel wieder erlauben?" : "Kill Switch aktivieren? Es werden keine neuen Trades eröffnet.")) return;
    await api("/api/kill-switch", { method: "POST", body: JSON.stringify({ active: !on, reason: on ? "" : "manuell im Dashboard" }) });
    toast(on ? "Kill Switch aufgehoben" : "Kill Switch aktiviert"); refresh();
  };

  // ------------------------------------------------------------- overview
  function kpi(label, value, sub, c = "") { return `<div class="kpi"><div class="label">${label}</div><div class="value ${c}">${value}</div><div class="sub">${sub || "&nbsp;"}</div></div>`; }
  async function renderOverview() {
    const o = overview;
    const status = { green: "Läuft", yellow: "Eingeschränkt", red: "Störung" }[o.health];
    $("#kpis").innerHTML = [
      kpi("Gesamtkapital", eur(o.equity), `Start ${eur(o.starting_capital, 0)} · Cash ${eur(o.cash, 0)}`),
      kpi("Tages-G&V", eur(o.daily_pnl), pct(o.daily_pnl_pct), cls(o.daily_pnl)),
      kpi("Gesamtrendite", pct(o.total_return_pct), `realisiert ${eur(o.realized_pnl)} · offen ${eur(o.unrealized_pnl)}`, cls(o.total_return_pct)),
      kpi("Aktive Trades", o.open_positions, `Exposure ${eur(o.gross_exposure, 0)} · ${o.closed_trades} abgeschlossen`),
      kpi("Analysierte Märkte", o.markets_analysed_last_cycle, `letzter Zyklus · ${o.deep_analysed_last_cycle} im Detail`),
      kpi("Systemstatus", `<span class="dot ${o.health}"></span> ${status}`, o.kill_switch.active ? "Kill Switch aktiv" : o.halt.active ? "Handelsstopp aktiv" : `${o.cycles} Zyklen`),
    ].join("");
    const c = o.last_cycle;
    if (c) {
      $("#cycle-meta").textContent = `#${c.id} · ${ago(c.finished_at || c.started_at)} · ${c.status}`;
      const names = { "1_marktdaten_scan": "Daten & Scan", "2_technische_analyse": "Technik", "3_signale": "Signale", "4_risikopruefung": "Risiko", "5_ausfuehrung": "Orders", "6_positionen": "Positionen", "7_speichern": "Speichern" };
      $("#pipeline").innerHTML = Object.entries(names).map(([k, label], i) => {
        const s = (c.steps || {})[k];
        const st = !s ? "" : s.ok ? "ok" : "err";
        return `<div class="step ${st}"><div class="n">0${i + 1}</div><div class="t">${label}</div><div class="s">${!s ? "–" : s.ok ? "✓ " + num(s.sec, 1) + " s" : "✗ Fehler"}</div></div>`;
      }).join("");
      $("#no-trade").innerHTML = `<b>${c.markets_scanned}</b> Märkte gescannt · <b>${c.analysed}</b> analysiert · <b>${c.signals}</b> Signale · <b>${c.approved}</b> freigegeben · <b>${c.executed}</b> ausgeführt` +
        (c.no_trade_reason ? `<br>Kein Trade: ${esc(c.no_trade_reason)}` : "") + (c.error ? `<br><span class="neg">Fehler: ${esc(c.error)}</span>` : "");
    } else { $("#pipeline").innerHTML = '<div class="empty">Noch kein Zyklus gelaufen – startet der Worker?</div>'; }
    const [pos, perf, logs] = await Promise.all([api("/api/positions?status=open"), api("/api/performance"), api("/api/logs?limit=60")]);
    renderOpenTable($("#open-table"), pos);
    const pts = perf.equity_curve.map((p) => ({ x: p.t, y: p.equity }));
    $("#eq-meta").textContent = pts.length ? `${pts.length} Punkte` : "";
    Charts.drawLine($("#eq-mini"), pts, { base: perf.starting_capital, fmt: (v) => num(v, 0) + " €", empty: "Noch keine Equity-Historie" });
    $("#feed").innerHTML = logs.length ? logs.map((l) => `<div class="feed-item ${l.level}"><span class="when">${hm(l.ts)}</span><span class="who">${esc(l.agent)}</span><span class="msg">${esc(l.message)}</span></div>`).join("") : '<div class="empty">Noch keine Entscheidungen</div>';
  }
  function renderOpenTable(el, pos) {
    if (!pos.length) { el.innerHTML = '<tr><td class="empty">Keine offenen Positionen. Das System eröffnet nur Trades, die alle Regeln und die Risikoprüfung bestehen.</td></tr>'; return; }
    el.innerHTML = `<thead><tr><th>Markt</th><th>Strategie</th><th>Seite</th><th class="num">Menge</th><th class="num">Einstieg</th><th class="num">Aktuell</th><th class="num">Stop</th><th class="num">Ziel</th><th class="num">G&V</th><th>Seit</th></tr></thead><tbody>` +
      pos.map((p) => `<tr class="click" data-pid="${p.id}"><td><b>${esc(p.symbol)}</b>${p.is_test ? ' <span class="tag a">TEST</span>' : ""}</td><td>${esc(p.strategy)}</td><td><span class="tag ${p.side === "long" ? "g" : "r"}">${p.side.toUpperCase()}</span></td><td class="num">${num(p.qty, 4)}</td><td class="num">${price(p.entry_price)}</td><td class="num">${price(p.last_price)}</td><td class="num neg">${price(p.stop_loss)}</td><td class="num pos">${price(p.take_profit)}</td><td class="num ${cls(p.unrealized_eur)}">${eur(p.unrealized_eur)}</td><td>${ago(p.entry_time)}</td></tr>`).join("") + "</tbody>";
    el.querySelectorAll("tr.click").forEach((tr) => (tr.onclick = () => openPosition(tr.dataset.pid)));
  }
  async function openPosition(pid) {
    const p = await api("/api/positions/" + pid);
    const r = p.rationale || {};
    const conds = (r.conditions || []).map((c) => `<span class="tag ${c.met ? "g" : "r"}">${c.met ? "✓" : "✗"} ${esc(c.label)}${c.value ? " · " + esc(c.value) : ""}</span>`).join("");
    modal(`<h2>Position #${p.id} · ${esc(p.symbol)} · ${esc(p.strategy)} ${p.side.toUpperCase()}</h2>
      <p class="note">${esc(p.rationale_text || "")}</p><div class="conds">${conds}</div>
      <div class="kv" style="margin-top:14px">
        <span>Status</span><span>${p.status}${p.exit_reason ? " – " + esc(p.exit_reason) : ""}</span>
        <span>Menge</span><span class="mono">${num(p.qty, 6)} (${eur(p.notional_eur)})</span>
        <span>Einstieg</span><span class="mono">${price(p.entry_price)} (Ref ${price(p.entry_ref_price)}) · ${dt(p.entry_time)}</span>
        <span>Stop / Ziel</span><span class="mono">${price(p.stop_loss)} / ${price(p.take_profit)}</span>
        <span>Ausstieg</span><span class="mono">${p.exit_price ? price(p.exit_price) + " · " + dt(p.exit_time) : "–"}</span>
        <span>Gebühren</span><span class="mono">${eur((p.entry_fee_eur || 0) + (p.exit_fee_eur || 0))}</span>
        <span>Slippage</span><span class="mono">${eur(p.slippage_eur)}</span>
        <span>Risiko</span><span class="mono">${eur(p.initial_risk_eur)}</span>
        <span>G&V</span><span class="mono ${cls(p.pnl_eur ?? p.unrealized_eur)}">${eur(p.pnl_eur ?? p.unrealized_eur)} ${p.r_multiple != null ? "(" + num(p.r_multiple, 2) + " R)" : ""}</span>
        <span>Order-ID</span><span class="mono" style="word-break:break-all">${esc(p.client_order_id)}</span>
      </div>
      ${p.exit_rationale ? `<p class="note">Ausstiegsregel: <b>${esc(p.exit_rationale.label)}</b> bei ${price(p.exit_rationale.trigger_price)} nach ${p.exit_rationale.held_min} min</p>` : ""}
      <h2 style="margin-top:16px">Orders</h2><div class="table-wrap"><table><thead><tr><th>Art</th><th>Seite</th><th class="num">Ref</th><th class="num">Fill</th><th class="num">Slippage</th><th class="num">Gebühr</th><th>Zeit</th></tr></thead><tbody>
      ${p.orders.map((o) => `<tr><td>${o.kind}</td><td>${o.side}</td><td class="num">${price(o.ref_price)}</td><td class="num">${price(o.fill_price)}</td><td class="num">${num(o.slippage_bps, 1)} bp</td><td class="num">${eur(o.fee_eur)}</td><td>${dt(o.created_at)}</td></tr>`).join("")}</tbody></table></div>`);
  }

  // ------------------------------------------------------------- scanner
  let scanCache = null;
  async function renderScanner() {
    scanCache = await api("/api/scanner?limit=150");
    drawScanTable();
  }
  function drawScanTable() {
    const s = scanCache; if (!s) return;
    $("#scan-meta").textContent = s.cycle ? `Zyklus #${s.cycle.id} · ${s.cycle.markets_scanned} Märkte · ${ago(s.cycle.started_at)}` : "";
    const f = $("#scan-filter").value.trim().toUpperCase(), ac = $("#scan-class").value;
    const items = s.items.filter((i) => (!f || i.symbol.includes(f)) && (!ac || i.asset_class === ac));
    const el = $("#scan-table");
    if (!items.length) { el.innerHTML = '<tr><td class="empty">Noch keine Scan-Ergebnisse</td></tr>'; return; }
    const arrow = (t) => t === "up" ? '<span class="pos">▲ auf</span>' : t === "down" ? '<span class="neg">▼ ab</span>' : t ? '<span class="muted">◆ seitw.</span>' : "–";
    el.innerHTML = `<thead><tr><th class="num">#</th><th>Markt</th><th>Typ</th><th class="num">Kurs</th><th class="num">24h / Tag</th><th class="num">Volumen</th><th class="num">Vol.-Faktor</th><th class="num">Bewegung σ</th><th>Trend 1h</th><th class="num">Score</th><th>Hinweise</th><th>Signale</th></tr></thead><tbody>` +
      items.map((i) => `<tr class="click" data-sym="${esc(i.symbol)}" data-ac="${i.asset_class}"><td class="num muted">${i.rank}</td><td><b>${esc(i.symbol)}</b></td><td><span class="tag">${i.asset_class === "crypto" ? "Krypto" : "Aktie"}</span></td><td class="num">${price(i.price)}</td><td class="num ${cls(i.change_pct)}">${pct(i.change_pct)}</td><td class="num">${compact(i.quote_volume)}</td><td class="num ${i.volume_ratio >= 2 ? "warn" : ""}">${i.volume_ratio != null ? num(i.volume_ratio, 2) + "×" : "–"}</td><td class="num ${Math.abs(i.move_z || 0) >= 2.5 ? "warn" : ""}">${i.move_z != null ? num(i.move_z, 1) : "–"}</td><td>${arrow(i.trend)}</td><td class="num">${num(i.score, 2)}</td><td>${(i.flags || []).map((x) => `<span class="tag a">${esc(x)}</span>`).join("")}${i.analysed ? '<span class="tag b">analysiert</span>' : ""}</td><td>${(i.signals || []).map((x) => `<span class="tag g">${esc(x)}</span>`).join("")}</td></tr>`).join("") + "</tbody>";
    el.querySelectorAll("tr.click").forEach((tr) => (tr.onclick = () => { term.symbol = tr.dataset.sym; term.ac = tr.dataset.ac; $("#sym-input").value = term.symbol; showTab("terminal"); }));
  }
  $("#scan-filter").oninput = drawScanTable; $("#scan-class").onchange = drawScanTable;

  // ------------------------------------------------------------- agents
  async function renderAgents() {
    const ag = await api("/api/agents");
    $("#agents").innerHTML = ag.map((a) => {
      const dot = STATUS_DOT[a.status] ?? "";
      const res = Object.entries(a.last_result || {}).filter(([k]) => !["scenarios", "model", "summary"].includes(k)).map(([k, v]) => `${k}: ${typeof v === "object" ? JSON.stringify(v) : v}`).join("\n");
      return `<div class="agent ${dot}"><div class="agent-head"><div><div class="agent-no">AGENT 0${a.no}</div><div class="agent-title">${esc(a.title)}</div></div><span class="pill"><i class="dot ${dot}"></i>${esc(a.status)}</span></div>
        <div class="agent-meta"><span>Aufgabe</span><span>${esc(a.current_task || "wartet auf nächsten Lauf")}</span><span>Aktivität</span><span>${ago(a.last_activity)}</span><span>Läufe</span><span class="mono">${a.runs} · Fehler ${a.errors}</span>${a.last_error ? `<span>Letzter Fehler</span><span class="neg">${esc(a.last_error)} (${ago(a.last_error_at)})</span>` : ""}</div>
        <div class="agent-result">${esc(res || "noch kein Ergebnis")}</div>
        <div class="agent-decisions">${a.decisions.map((d) => `<div class="${d.level === "ERROR" ? "neg" : d.level === "WARNING" ? "warn" : ""}"><span class="when">${hm(d.ts)}</span>${esc(d.message)}</div>`).join("") || '<div class="muted">Noch keine Entscheidungen</div>'}</div></div>`;
    }).join("");
  }

  // ------------------------------------------------------------- terminal
  const term = { symbol: localStorageGet("sym") || null, tf: "1h", ac: null, chart: null };
  document.querySelectorAll("#tf-seg button").forEach((b) => (b.onclick = () => { document.querySelectorAll("#tf-seg button").forEach((x) => x.classList.remove("active")); b.classList.add("active"); term.tf = b.dataset.tf; renderTerminal(true); }));
  document.querySelectorAll("#ind-seg button").forEach((b) => (b.onclick = () => { b.classList.toggle("active"); term.chart.show[b.dataset.ind] = b.classList.contains("active"); term.chart.draw(); }));
  $("#sym-input").onchange = () => { term.symbol = $("#sym-input").value.trim().toUpperCase(); term.ac = null; renderTerminal(true); };
  async function renderTerminal(fresh) {
    if (!term.chart) term.chart = new Charts.CandleChart($("#chart"));
    const [pos, sigs, scan] = await Promise.all([api("/api/positions?status=open"), api("/api/signals?limit=30"), scanCache ? Promise.resolve(scanCache) : api("/api/scanner?limit=150")]);
    scanCache = scan;
    const syms = [...new Set([...pos.map((p) => p.symbol), ...scan.items.map((i) => i.symbol)])];
    $("#sym-list").innerHTML = syms.map((s) => `<option value="${esc(s)}">`).join("");
    if (!term.symbol) term.symbol = (pos[0] && pos[0].symbol) || (scan.items[0] && scan.items[0].symbol);
    if (term.symbol) {
      $("#sym-input").value = term.symbol; localStorageSet("sym", term.symbol);
      try {
        const d = await api(`/api/candles?symbol=${encodeURIComponent(term.symbol)}&tf=${term.tf}&limit=400${term.ac ? "&asset_class=" + term.ac : ""}`);
        term.chart.setData(d, !fresh);
        const last = d.c[d.c.length - 1];
        $("#chart-meta").innerHTML = `${esc(d.source)} · letzter Kurs <b class="mono">${price(last)}</b>${d.stale ? ' · <span class="warn">Daten veraltet / Markt geschlossen</span>' : ""}`;
      } catch (e) { $("#chart-meta").innerHTML = `<span class="neg">${esc(e.message)}</span>`; }
      const a = await api("/api/analysis/" + encodeURIComponent(term.symbol));
      renderAnalysis(a);
    }
    $("#chart-legend").innerHTML = '<span><i style="background:#f5b942"></i>EMA20</span><span><i style="background:#5aa9ff"></i>EMA50</span><span><i style="background:#b48cff"></i>EMA200</span><span><i style="background:rgba(230,233,236,.4)"></i>Bollinger 20/2</span><span><i style="background:#3ddc84"></i>TP / Long</span><span><i style="background:#ff5f56"></i>SL / Short</span><span>Ziehen = verschieben · Pinch/Mausrad = Zoom · Tippen = Fadenkreuz</span>';
    renderOpenTable($("#term-open"), pos);
    renderSignals(sigs);
  }
  function renderAnalysis(a) {
    if (!a || !a.tf) { $("#term-analysis").innerHTML = '<div class="empty">Für diesen Markt liegt noch keine Agenten-Analyse vor (nur Top-Märkte werden im Detail analysiert).</div>'; return; }
    const rows = Object.entries(a.tf).map(([tf, s]) => `<tr><td><b>${tf}</b></td><td>${s.trend === "up" ? '<span class="pos">▲</span>' : s.trend === "down" ? '<span class="neg">▼</span>' : "◆"} ${esc(s.structure || "")}</td><td class="num">${num(s.rsi, 1)}</td><td class="num ${cls(s.macd_hist)}">${s.macd_hist != null ? Number(s.macd_hist).toPrecision(3) : "–"}</td><td class="num">${num(s.bb_pctb, 2)}</td><td class="num">${num(s.atr_pct, 2)} %</td><td class="num">${num(s.adx, 0)}</td><td class="num">${num(s.volume_ratio, 2)}×</td><td>${(s.patterns || []).map((p) => `<span class="tag">${esc(p)}</span>`).join("")}${s.stale ? '<span class="tag a">veraltet</span>' : ""}</td></tr>`).join("");
    const h1 = a.tf["1h"] || {};
    $("#term-analysis").innerHTML = `<div class="muted" style="margin-bottom:8px">Multi-Timeframe-Ausrichtung <b class="mono ${cls(a.alignment)}">${num(a.alignment, 2)}</b> · Unterstützung 1h <b class="mono">${price(h1.support)}</b> · Widerstand 1h <b class="mono">${price(h1.resistance)}</b></div><div class="table-wrap"><table><thead><tr><th>TF</th><th>Trend/Struktur</th><th class="num">RSI</th><th class="num">MACD-H</th><th class="num">%B</th><th class="num">ATR</th><th class="num">ADX</th><th class="num">Vol</th><th>Muster</th></tr></thead><tbody>${rows}</tbody></table></div>`;
  }
  function renderSignals(sigs) {
    if (!sigs.length) { $("#signals").innerHTML = '<div class="empty">Noch keine Signale. Wenn keine Strategie alle Bedingungen erfüllt, dokumentiert der Zyklus den Grund (siehe Übersicht / System).</div>'; return; }
    const stTag = { executed: "g", approved: "g", vetoed: "r", failed: "r", proposed: "b" };
    $("#signals").innerHTML = sigs.map((g) => {
      const r = g.rationale || {}, m = (r.model || {}).pooled || {};
      const conds = (r.conditions || []).map((c) => `<span class="tag ${c.met ? "g" : "r"}">${c.met ? "✓" : "✗"} ${esc(c.label)}${c.value ? " · " + esc(c.value) : ""}</span>`).join("");
      const checks = ((g.risk_notes || {}).checks || []).map((c) => `<span class="${c.ok ? "pos" : "neg"}">${c.ok ? "✓" : "✗"}</span><span>${esc(c.label)}</span><span class="v">${esc(c.value)}</span>`).join("");
      return `<div class="signal"><div class="signal-head"><b>${esc(g.symbol)}</b><span class="tag ${g.side === "long" ? "g" : "r"}">${g.side.toUpperCase()}</span><span class="tag">${esc(g.strategy)} · ${g.timeframe}</span><span class="tag ${stTag[g.status] || ""}">${esc(g.status)}</span>${g.is_test ? '<span class="tag a">TEST</span>' : ""}<span class="muted">${dt(g.created_at)}</span></div>
        <div class="signal-text">${esc(r.text || "")}</div>
        <div class="muted mono" style="margin-top:6px">p ${g.probability != null ? num(g.probability * 100, 0) + " %" : "–"} [${g.prob_ci[0] != null ? num(g.prob_ci[0] * 100, 0) : "–"}–${g.prob_ci[1] != null ? num(g.prob_ci[1] * 100, 0) : "–"} %] · n=${g.sample_size} · EV ${g.expected_value_r != null ? num(g.expected_value_r, 2) + " R" : "–"} · CRV ${num(g.reward_risk, 2)} · Kosten ${num(g.cost_r, 2)} R · TP/SL/Zeit im Replay ${m.tp_hits ?? "–"}/${m.sl_hits ?? "–"}/${m.time_exits ?? "–"}</div>
        <div class="conds">${conds}</div>
        ${g.veto_reason ? `<div class="note neg">Veto: ${esc(g.veto_reason)}</div>` : ""}
        ${checks ? `<details><summary>Risikoprüfung (${((g.risk_notes || {}).checks || []).length} Checks)</summary><div class="checks">${checks}</div></details>` : ""}
        ${r.model ? `<details><summary>Modell-Details</summary><div class="note">${esc(r.model.source || "")}<br>Gesamt: n=${m.n}, Treffer ${m.wins}, Ø ${num(m.mean_r, 3)} R, Median ${num(m.median_r, 3)} R, PF ${num(m.profit_factor, 2)}<br>Nur ${esc(g.symbol)}: n=${(r.model.symbol || {}).n}, Ø ${num((r.model.symbol || {}).mean_r, 3)} R<br>Round-Trip-Kosten ${num(r.model.cost_pct_round_trip, 3)} %</div></details>` : ""}
      </div>`;
    }).join("");
  }

  // ------------------------------------------------------------- performance
  async function renderPerformance() {
    const [p, hist] = await Promise.all([api("/api/performance"), api("/api/positions?status=closed&limit=500")]);
    const t = p.total;
    const wr = t.trades ? `${num(t.win_rate * 100, 1)} %` : "–";
    const ci = t.trades ? `95 %-KI ${num(t.win_rate_ci[0] * 100, 0)}–${num(t.win_rate_ci[1] * 100, 0)} %` : "noch keine Trades";
    $("#perf-kpis").innerHTML = [
      kpi("Trades", t.trades, t.trades ? `${t.wins} Gewinner · ${t.losses} Verlierer` : "abgeschlossen"),
      kpi("Gewinnquote", wr, ci),
      kpi("Profit Factor", t.trades ? num(t.profit_factor, 2) : "–", "Bruttogewinn / Bruttoverlust"),
      kpi("Max. Drawdown", pct(-(t.max_drawdown_pct_equity || 0), 2, false), "auf Equity-Basis", "neg"),
      kpi("Netto-G&V", eur(t.net_pnl_eur || 0), t.trades ? `Ø ${eur(t.expectancy_eur)} · ${num(t.expectancy_r, 2)} R` : "", cls(t.net_pnl_eur)),
      kpi("Kosten", eur((t.fees_eur || 0) + (t.slippage_eur || 0)), `Gebühren ${eur(t.fees_eur || 0)} · Slippage ${eur(t.slippage_eur || 0)}`),
    ].join("");
    Charts.drawLine($("#eq-big"), p.equity_curve.map((x) => ({ x: x.t, y: x.equity })), { base: p.starting_capital, fmt: (v) => num(v, 0) + " €" });
    Charts.drawLine($("#dd-big"), p.drawdown_curve.map((x) => ({ x: x.t, y: x.dd })), { color: "#ff5f56", fmt: (v) => num(v, 1) + " %", fillTo: 0, empty: "Drawdown" });
    $("#strat-table").innerHTML = `<thead><tr><th>Strategie</th><th>TF</th><th>Aktiv</th><th class="num">Trades</th><th class="num">Trefferquote</th><th class="num">PF</th><th class="num">Ø R</th><th class="num">Netto</th><th class="num">Max DD</th><th>Status</th></tr></thead><tbody>` +
      p.strategies.map((s) => { const m = s.metrics || {}; return `<tr><td><b>${esc(s.label)}</b><div class="muted" style="white-space:normal;max-width:420px">${esc(s.description)}</div></td><td class="mono">${esc(s.timeframes.crypto)}/${esc(s.timeframes.stock)}</td><td><button class="switch ${s.enabled ? "on" : ""}" data-s="${s.name}" data-on="${s.enabled ? 1 : 0}" aria-label="umschalten"></button></td><td class="num">${m.trades || 0}</td><td class="num">${m.trades ? num(m.win_rate * 100, 1) + " %" : "–"}</td><td class="num">${m.trades ? num(m.profit_factor, 2) : "–"}</td><td class="num ${cls(m.expectancy_r)}">${m.trades ? num(m.expectancy_r, 2) : "–"}</td><td class="num ${cls(m.net_pnl_eur)}">${m.trades ? eur(m.net_pnl_eur) : "–"}</td><td class="num">${m.trades ? num(m.max_drawdown_pct, 2) + " %" : "–"}</td><td style="white-space:normal">${s.enabled ? '<span class="pos">aktiv</span>' : `<span class="neg">${esc(s.disabled_reason || "deaktiviert")}</span>`}</td></tr>`; }).join("") + "</tbody>";
    document.querySelectorAll("#strat-table .switch").forEach((b) => (b.onclick = async () => {
      const on = b.dataset.on === "1";
      if (!confirm(`Strategie ${b.dataset.s} ${on ? "deaktivieren" : "aktivieren"}?`)) return;
      await api("/api/strategies/" + b.dataset.s, { method: "POST", body: JSON.stringify({ enabled: !on }) }); renderPerformance();
    }));
    const f = (p.report && p.report.findings) || [];
    $("#findings").innerHTML = f.length ? f.map((x) => `<div class="finding ${x.severity}"><div class="t">${esc(x.title)}${x.statistically_weak ? ' <span class="tag a">statistisch schwach (n=' + x.sample_size + ")</span>" : ""}</div><div class="e">Beleg: ${esc(x.evidence)}</div><div class="s">Vorschlag (im Forward-Test überprüfen): ${esc(x.suggestion)}</div></div>`).join("") : '<div class="empty">Noch keine Auffälligkeiten – dafür braucht es abgeschlossene Trades.</div>';
    const el = $("#hist-table");
    el.innerHTML = hist.length ? `<thead><tr><th>Geschlossen</th><th>Markt</th><th>Strategie</th><th>Seite</th><th class="num">Einstieg</th><th class="num">Ausstieg</th><th>Grund</th><th class="num">Kosten</th><th class="num">G&V</th><th class="num">R</th></tr></thead><tbody>` +
      hist.map((h) => `<tr class="click" data-pid="${h.id}"><td>${dt(h.exit_time)}</td><td><b>${esc(h.symbol)}</b></td><td>${esc(h.strategy)}</td><td><span class="tag ${h.side === "long" ? "g" : "r"}">${h.side.toUpperCase()}</span></td><td class="num">${price(h.entry_price)}</td><td class="num">${price(h.exit_price)}</td><td>${esc(h.exit_reason)}</td><td class="num">${eur((h.entry_fee_eur || 0) + (h.exit_fee_eur || 0) + (h.slippage_eur || 0))}</td><td class="num ${cls(h.pnl_eur)}">${eur(h.pnl_eur)}</td><td class="num ${cls(h.r_multiple)}">${num(h.r_multiple, 2)}</td></tr>`).join("") + "</tbody>"
      : '<tr><td class="empty">Noch keine abgeschlossenen Trades</td></tr>';
    el.querySelectorAll("tr.click").forEach((tr) => (tr.onclick = () => openPosition(tr.dataset.pid)));
  }

  // ------------------------------------------------------------- system
  async function renderSystem() {
    const [h, cyc, gate, st, errs] = await Promise.all([api("/api/health"), api("/api/cycles?limit=40"), api("/api/live-gate"), api("/api/selftest"), api("/api/logs?level=ERROR&limit=40")]);
    $("#health-meta").textContent = h.worker && h.worker.ts ? `Worker ${h.worker.host || ""} · PID ${h.worker.pid} · seit ${dt(h.worker.started_at)}` : "kein Worker";
    $("#components").innerHTML = h.components.map((c) => `<div class="comp"><span class="dot ${c.status}"></span><div><div class="name">${esc(c.name)}</div><div class="detail">${esc(c.detail)}</div></div></div>`).join("");
    const ks = overview.kill_switch;
    $("#ks-state").textContent = ks.active ? `AKTIV seit ${dt(ks.at)} – ${ks.reason}${ks.flatten ? " (Glattstellung)" : ""}` : "nicht aktiv";
    $("#halt-state").textContent = overview.halt.active ? overview.halt.reason : "kein Stopp aktiv";
    const r = st.result;
    $("#selftest").innerHTML = (st.running && st.running.running ? '<div class="warn">Selbsttest läuft …</div>' : "") + (r && r.checks ? `<div class="muted">Letzter Lauf ${dt(r.at)} · ${r.ok ? '<span class="pos">BESTANDEN</span>' : '<span class="neg">FEHLGESCHLAGEN</span>'} · ${r.duration_sec} s</div><div class="checks" style="margin-top:6px">${r.checks.map((c) => `<span class="${c.ok ? "pos" : "neg"}">${c.ok ? "✓" : "✗"}</span><span>${esc(c.name)}</span><span class="v">${esc(typeof c.detail === "string" ? c.detail : "")}</span>`).join("")}</div>` : '<div class="muted">Noch nicht ausgeführt</div>');
    $("#live-gate").innerHTML = `<p class="note">${esc(gate.note)} Alle Voraussetzungen müssen erfüllt sein, bevor überhaupt ein Live-Adapter gebaut und aktiviert werden darf.</p><div class="gate">${gate.checks.map((c) => `<span class="${c.ok ? "pos" : "neg"}">${c.ok ? "✓" : "✗"}</span><span>${esc(c.label)}</span><span class="v">${esc(c.value)}</span>`).join("")}</div>`;
    $("#cycles-table").innerHTML = cyc.length ? `<thead><tr><th>#</th><th>Start</th><th>Status</th><th class="num">Dauer</th><th class="num">Märkte</th><th class="num">Analysiert</th><th class="num">Signale</th><th class="num">Frei</th><th class="num">Orders</th><th>Schritte</th><th>Begründung</th></tr></thead><tbody>` +
      cyc.map((c) => `<tr><td class="mono">${c.id}</td><td>${dt(c.started_at)}</td><td><span class="tag ${c.status === "ok" ? "g" : c.status === "running" ? "b" : "r"}">${c.status}</span></td><td class="num">${c.finished_at ? num(c.finished_at - c.started_at, 1) + " s" : "–"}</td><td class="num">${c.markets_scanned}</td><td class="num">${c.analysed}</td><td class="num">${c.signals}</td><td class="num">${c.approved}</td><td class="num">${c.executed}</td><td>${Object.entries(c.steps || {}).map(([k, s]) => `<span class="tag ${s.ok ? "g" : "r"}" title="${esc(k)}">${k.slice(0, 1)}</span>`).join("")}</td><td style="white-space:normal;min-width:280px" class="muted">${esc(c.error || c.no_trade_reason || "")}</td></tr>`).join("") + "</tbody>"
      : '<tr><td class="empty">Noch keine Zyklen</td></tr>';
    $("#errors").innerHTML = errs.length ? errs.map((l) => `<div class="feed-item ERROR"><span class="when">${hm(l.ts)}</span><span class="who">${esc(l.agent)}</span><span class="msg">${esc(l.message)}</span></div>`).join("") : '<div class="empty">Keine Fehler protokolliert</div>';
  }
  const post = (u, b) => api(u, { method: "POST", body: JSON.stringify(b || {}) });
  $("#ks-on").onclick = async () => { if (confirm("Kill Switch aktivieren?")) { await post("/api/kill-switch", { active: true, reason: "manuell (System-Tab)" }); toast("Kill Switch aktiv"); refresh(); } };
  $("#ks-flat").onclick = async () => { if (confirm("Kill Switch aktivieren UND alle offenen Positionen zum Marktpreis schließen?")) { await post("/api/kill-switch", { active: true, flatten: true, reason: "manuell + Glattstellung" }); toast("Kill Switch aktiv, Positionen werden geschlossen"); refresh(); } };
  $("#ks-off").onclick = async () => { if (confirm("Kill Switch aufheben?")) { await post("/api/kill-switch", { active: false }); toast("Kill Switch aufgehoben"); refresh(); } };
  $("#ks-test").onclick = async () => { const r = await post("/api/kill-switch/test"); toast(r.ok ? "Test bestanden: " + r.detail : "TEST FEHLGESCHLAGEN"); refresh(); };
  $("#halt-reset").onclick = async () => { if (confirm("Handelsstopp manuell aufheben? Der Drawdown-Bezugspunkt wird zurückgesetzt.")) { await post("/api/halt/reset"); toast("Freigegeben"); refresh(); } };
  $("#st-run").onclick = async () => { const r = await post("/api/selftest"); toast(r.started ? "Selbsttest gestartet (ca. 10–60 s)" : r.message); setTimeout(refresh, 4000); };

  // ------------------------------------------------------------- loop
  let busy = false, again = false;
  async function refresh() {
    if (busy) { again = true; return; } // e.g. tab switched while loading -> render once more afterwards
    busy = true; again = false;
    try {
      await loadOverview();
      if (active === "overview") await renderOverview();
      else if (active === "scanner") await renderScanner();
      else if (active === "agents") await renderAgents();
      else if (active === "terminal") await renderTerminal(false);
      else if (active === "performance") await renderPerformance();
      else if (active === "system") await renderSystem();
    } catch (e) { if (e.message !== "auth") toast("Fehler: " + e.message); console.error(e); }
    finally { busy = false; if (again) setTimeout(refresh, 0); }
  }
  showTab(active);
  setInterval(() => { if (!document.hidden) refresh(); }, 10000);
  document.addEventListener("visibilitychange", () => { if (!document.hidden) refresh(); });
  window.addEventListener("resize", () => { if (active === "overview" || active === "performance") refresh(); });
})();
