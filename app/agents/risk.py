"""Agent 4 - Risk Manager (unrestricted veto).

Every signal passes a list of hard checks. One failed check = veto. The agent
also enforces account-level limits (daily loss, max drawdown, repeated errors)
and can halt all trading via the kill switch.
"""
from __future__ import annotations

import math

from sqlalchemy.orm import Session

from app.agents.base import Agent
from app.config import get_settings
from app.data.hub import MarketData
from app.db import Order, Position, Signal, get_state, now, session_scope, set_state
from app.engine.costs import fee_eur, slippage_bps
from app.engine.portfolio import ACCOUNT_LOCK, account_snapshot, get_account, today_str
from app.strategies import STRATEGIES

KILL_KEY = "kill_switch"


def halt_key(account: str) -> str:
    return f"halt:{account}"


def kill_switch_state(s: Session) -> dict:
    return get_state(s, KILL_KEY, {"active": False})


def set_kill_switch(s: Session, active: bool, reason: str, by: str, flatten: bool = False) -> dict:
    v = {"active": active, "reason": reason, "by": by, "at": now(), "flatten": bool(flatten and active)}
    set_state(s, KILL_KEY, v)
    return v


def client_order_id(account: str, sig: Signal) -> str:
    bar = int(sig.bar_time or (sig.rationale or {}).get("bar_time") or sig.created_at)
    return f"{account}:{sig.strategy}:{sig.symbol}:{sig.side}:{bar}"


class RiskManager(Agent):
    name = "risk"

    def __init__(self, md: MarketData, prefix: str = ""):
        super().__init__(prefix)
        self.md = md
        self.st = get_settings()

    # -- account level ----------------------------------------------------------------
    def check_account_limits(self, account: str) -> dict:
        """Daily loss -> halt until tomorrow; drawdown -> halt until manual reset."""
        try:
            return self._check_account_limits(account)
        finally:
            if not self._in_task:
                self.flush()

    def _check_account_limits(self, account: str) -> dict:
        with ACCOUNT_LOCK, session_scope() as s:
            acc = get_account(s, account)
            snap = account_snapshot(s, acc)
            eq = snap["equity"]
            if eq > acc.peak_equity:
                acc.peak_equity = eq
            if acc.day_start_date != today_str():
                acc.day_start_date = today_str()
                acc.day_start_equity = eq
            daily = (eq / acc.day_start_equity - 1) * 100 if acc.day_start_equity else 0.0
            dd = (1 - eq / acc.peak_equity) * 100 if acc.peak_equity else 0.0
            halt = get_state(s, halt_key(account), {"active": False})
            if halt.get("active") and halt.get("kind") == "daily_loss" and halt.get("date") != today_str():
                halt = {"active": False, "reason": "neuer Handelstag", "at": now()}
                set_state(s, halt_key(account), halt)
                self.decision(f"[{account}] Tagesverlust-Sperre aufgehoben (neuer Tag).")
            if not halt.get("active"):
                if dd >= self.st.max_drawdown_pct:
                    halt = {"active": True, "kind": "drawdown", "at": now(),
                            "reason": f"Max. Drawdown {dd:.2f} % ≥ {self.st.max_drawdown_pct} % – manuelle Freigabe nötig"}
                    set_state(s, halt_key(account), halt)
                    set_kill_switch(s, True, halt["reason"], "risk")
                    self.decision(f"[{account}] NOTSTOPP: {halt['reason']}", level="WARNING")
                elif daily <= -self.st.max_daily_loss_pct:
                    halt = {"active": True, "kind": "daily_loss", "date": today_str(), "at": now(),
                            "reason": f"Tagesverlust {daily:.2f} % ≤ -{self.st.max_daily_loss_pct} % – Pause bis morgen"}
                    set_state(s, halt_key(account), halt)
                    self.decision(f"[{account}] Handel pausiert: {halt['reason']}", level="WARNING")
            return {"equity": eq, "daily_pct": daily, "drawdown_pct": dd, "halt": halt}

    def register_cycle_result(self, ok: bool, account: str) -> None:
        try:
            self._register_cycle_result(ok, account)
        finally:
            self.flush()

    def _register_cycle_result(self, ok: bool, account: str) -> None:
        key = f"consecutive_errors:{account}"
        with session_scope() as s:
            st = get_state(s, key, {"n": 0})
            n = 0 if ok else st.get("n", 0) + 1
            set_state(s, key, {"n": n, "at": now()})
            if n >= self.st.max_consecutive_errors and not kill_switch_state(s).get("active"):
                set_kill_switch(s, True, f"{n} fehlgeschlagene Zyklen in Folge", "risk")
                self.decision(f"NOTSTOPP: {n} fehlgeschlagene Zyklen in Folge – Kill Switch aktiviert.", level="WARNING")

    # -- signal level -------------------------------------------------------------------
    def review(self, cycle_id: int, signal_ids: list[int], account: str) -> tuple[list[int], dict]:
        self.cycle_id = cycle_id
        with self.task(f"{len(signal_ids)} Signal(e) prüfen") as res:
            limits = self.check_account_limits(account)
            approved: list[int] = []
            vetoes: list[str] = []
            with session_scope() as s:
                sigs = s.query(Signal).filter(Signal.id.in_(signal_ids)).all() if signal_ids else []
                sigs.sort(key=lambda x: -(x.expected_value_r if x.expected_value_r is not None else -9))
                planned_exposure = 0.0
                planned_symbols: set[str] = set()
                planned_count = 0
                for sig in sigs:
                    ok, checks, sizing = self._evaluate(s, sig, account, limits, planned_exposure,
                                                        planned_symbols, planned_count)
                    sig.risk_notes = {"checks": checks, "sizing": sizing, "limits": {
                        "daily_pct": round(limits["daily_pct"], 3), "drawdown_pct": round(limits["drawdown_pct"], 3)}}
                    if ok:
                        sig.status = "approved"
                        approved.append(sig.id)
                        planned_exposure += sizing["notional_eur"]
                        planned_symbols.add(sig.symbol)
                        planned_count += 1
                        self.decision(f"FREIGABE {sig.side.upper()} {sig.symbol} ({sig.strategy}): "
                                      f"{sizing['qty']:.6g} Stk., Risiko {sizing['risk_eur']:.2f} €, "
                                      f"Volumen {sizing['notional_eur']:.2f} €.", {"signal_id": sig.id, "sizing": sizing})
                    else:
                        failed = [c for c in checks if not c["ok"]]
                        sig.status = "vetoed"
                        sig.veto_reason = "; ".join(f"{c['label']}: {c['value']}" for c in failed)
                        vetoes.append(f"{sig.symbol}/{sig.strategy}: {sig.veto_reason}")
                        self.decision(f"VETO {sig.side.upper()} {sig.symbol} ({sig.strategy}): {sig.veto_reason}",
                                      {"signal_id": sig.id, "checks": checks})
            res.update({"reviewed": len(signal_ids), "approved": len(approved), "vetoed": len(vetoes),
                        "vetoes": vetoes[:10], "daily_pnl_pct": round(limits["daily_pct"], 3),
                        "drawdown_pct": round(limits["drawdown_pct"], 3), "halt": limits["halt"]})
            if limits["halt"].get("active"):
                res["_status"] = "halted"
            return approved, res

    def _evaluate(self, s: Session, sig: Signal, account: str, limits: dict, planned_exposure: float,
                  planned_symbols: set[str], planned_count: int) -> tuple[bool, list[dict], dict]:
        st = self.st
        checks: list[dict] = []

        def chk(key: str, label: str, ok: bool, value) -> bool:
            checks.append({"key": key, "label": label, "ok": bool(ok), "value": value})
            return bool(ok)

        ks = kill_switch_state(s)
        chk("kill_switch", "Kill Switch aus", not ks.get("active"), ks.get("reason") or "aus")
        halt = limits["halt"]
        chk("halt", "Kein Handelsstopp (Tagesverlust/Drawdown)", not halt.get("active"), halt.get("reason") or "ok")
        if not all(c["ok"] for c in checks):
            return False, checks, {}  # global stop: no further checks / market data requests needed
        strat = STRATEGIES[sig.strategy]
        from app.db import StrategyState

        ss = s.get(StrategyState, sig.strategy)
        chk("strategy_enabled", "Strategie aktiv", ss is None or ss.enabled, (ss.disabled_reason if ss else None) or "aktiv")

        acc = get_account(s, account)
        snap = account_snapshot(s, acc)
        equity = snap["equity"]
        opens = snap["positions"]
        dup = any(p.symbol == sig.symbol for p in opens) or sig.symbol in planned_symbols
        chk("no_duplicate", "Keine offene Position im selben Markt", not dup, "bereits offen" if dup else "ok")
        cid = client_order_id(account, sig)
        exists = s.query(Order.id).filter(Order.client_order_id == cid).first() is not None
        chk("idempotent", "Signal noch nicht ausgeführt", not exists, cid)
        n_open = len(opens) + planned_count
        chk("max_positions", f"Max. {st.max_open_positions} offene Positionen", n_open < st.max_open_positions, n_open)

        last_loss = (s.query(Position.exit_time).filter(Position.account == account, Position.symbol == sig.symbol,
                                                         Position.status == "closed", Position.pnl_eur < 0)
                     .order_by(Position.exit_time.desc()).first())
        cooldown_ok = not last_loss or now() - last_loss[0] > st.loss_cooldown_min * 60
        chk("cooldown", f"Keine Verlust-Abkühlphase ({st.loss_cooldown_min} min)", cooldown_ok,
            "ok" if cooldown_ok else f"Verlust vor {(now() - last_loss[0]) / 60:.0f} min")

        # --- live price & freshness ---
        price = None
        price_age = None
        market_open = True
        try:
            market_open = self.md.is_market_open(sig.symbol, sig.asset_class)
            price, pts = self.md.last_price(sig.symbol, sig.asset_class)
            price_age = self.md.clock() - pts
        except Exception as e:  # noqa: BLE001
            chk("price", "Aktueller Kurs verfügbar", False, str(e)[:120])
        chk("market_open", "Markt geöffnet", market_open, "offen" if market_open else "geschlossen")
        if price is not None:
            chk("fresh", f"Kurs aktuell (≤ {st.stale_price_sec} s)", price_age is not None and price_age <= st.stale_price_sec,
                f"{price_age:.0f} s alt")
        sizing: dict = {}
        if price is None:
            return False, checks, sizing

        atr = (sig.rationale or {}).get("indicators", {}).get("atr") or abs(sig.entry - sig.stop_loss)
        drift = abs(price - sig.entry)
        chk("drift", "Kurs nahe Signal (≤ 0,5 ATR)", drift <= 0.5 * atr, f"{drift / atr:.2f} ATR" if atr else "n/a")
        side_ok = (sig.stop_loss < price < sig.take_profit) if sig.side == "long" else (sig.take_profit < price < sig.stop_loss)
        chk("levels", "Stop/Ziel auf richtiger Seite des Kurses", side_ok,
            f"SL {sig.stop_loss:.6g} / Kurs {price:.6g} / TP {sig.take_profit:.6g}")

        # --- statistical evidence ---
        chk("samples", f"Ausreichende Datenbasis (n ≥ {st.min_model_samples})", sig.sample_size >= st.min_model_samples,
            f"n={sig.sample_size}")
        ev = sig.expected_value_r
        chk("ev", f"Erwartungswert nach Kosten ≥ {st.min_expected_value_r} R", ev is not None and ev >= st.min_expected_value_r,
            f"{ev:+.3f} R" if ev is not None else "unbekannt")

        risk_unit = abs(price - sig.stop_loss)
        rr = abs(sig.take_profit - price) / risk_unit if risk_unit > 0 else 0.0
        min_rr = strat.min_rr if strat.min_rr is not None else st.min_reward_risk
        chk("rr", f"Chance/Risiko ≥ {min_rr}", rr >= min_rr, f"{rr:.2f}")

        # --- sizing ---
        try:
            fx = self.md.eur_per(self.md.quote_ccy(sig.symbol, sig.asset_class))
        except Exception as e:  # noqa: BLE001
            chk("fx", "Wechselkurs verfügbar", False, str(e)[:120])
            return False, checks, sizing
        qv = (sig.rationale or {}).get("quote_volume")
        risk_budget = equity * st.risk_per_trade_pct / 100
        approx_notional = equity * st.max_position_pct / 100
        slip = slippage_bps(sig.asset_class, qv, approx_notional, st) / 10_000
        fee_rate = fee_eur(sig.asset_class, approx_notional, st) / max(approx_notional, 1)
        per_unit = risk_unit * fx + 2 * price * fx * (slip + fee_rate)
        qty = risk_budget / per_unit if per_unit > 0 else 0.0
        caps = {"risk": qty}
        caps["max_position"] = equity * st.max_position_pct / 100 / (price * fx)
        remaining_gross = equity * st.max_gross_exposure_pct / 100 - snap["gross_exposure"] - planned_exposure
        caps["gross_exposure"] = max(0.0, remaining_gross) / (price * fx)
        if sig.side == "long":
            caps["cash"] = max(0.0, (acc.cash - planned_exposure) * 0.995) / (price * fx * (1 + slip + fee_rate))
        qty = max(0.0, min(caps.values()))
        binding = min(caps, key=caps.get)
        if sig.asset_class == "stock":
            qty = math.floor(qty)
        else:
            mag = 10 ** math.floor(math.log10(qty)) if qty > 0 else 1
            qty = math.floor(qty / mag * 1000) / 1000 * mag  # 4 significant digits, rounded down
        notional = qty * price * fx
        risk_eur = qty * per_unit
        sizing = {"qty": qty, "price": price, "fx": fx, "notional_eur": round(notional, 2), "risk_eur": round(risk_eur, 2),
                  "risk_budget_eur": round(risk_budget, 2), "binding_limit": binding, "equity": round(equity, 2),
                  "rr_at_price": round(rr, 3), "price_age_sec": round(price_age or 0, 1), "client_order_id": cid}
        chk("size", "Positionsgröße ≥ 10 € (nach Limits)", notional >= 10 and qty > 0,
            f"{notional:.2f} € (begrenzt durch {binding})")
        return all(c["ok"] for c in checks), checks, sizing
