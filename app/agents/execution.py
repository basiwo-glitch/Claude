"""Agent 5 - Execution Agent: paper orders, position monitoring, rule-based exits.

Exit rules (checked on every 1-minute bar since the last check + the live price):
* stop-loss: filled at the stop, or at the bar's open if the market gapped through
  it, plus adverse slippage
* take-profit: limit fill at the target (no slippage)
* stop and target inside the same bar -> stop assumed first (conservative)
* time stop: max. holding time of the strategy -> market exit
* kill switch with "flatten" -> market exit
"""
from __future__ import annotations

import time

from app.agents.base import Agent
from app.agents.risk import client_order_id, kill_switch_state
from app.brokers.base import Fill
from app.brokers.paper import DuplicateOrder, PaperBroker
from app.config import get_settings
from app.data.base import TF_SECONDS
from app.data.hub import MarketData
from app.db import Position, Signal, now, session_scope
from app.engine.costs import fee_eur, slippage_bps
from app.engine.portfolio import ACCOUNT_LOCK, get_account, record_equity
from app.strategies import STRATEGIES


class ExecutionAgent(Agent):
    name = "execution"

    def __init__(self, md: MarketData, broker: PaperBroker | None = None, prefix: str = ""):
        super().__init__(prefix)
        self.md = md
        self.broker = broker or PaperBroker()  # live broker is never used here (see brokers/live.py)
        self.st = get_settings()

    # -- entries --------------------------------------------------------------------
    def execute(self, cycle_id: int, approved_ids: list[int], account: str) -> tuple[list[int], dict]:
        self.cycle_id = cycle_id
        with self.task(f"{len(approved_ids)} Order(s) ausführen") as res:
            done, failed = [], []
            for sid in approved_ids:
                try:
                    pid = self._execute_one(sid, account)
                    if pid:
                        done.append(pid)
                except Exception as e:  # noqa: BLE001
                    failed.append(f"Signal {sid}: {e}")
                    self.decision(f"Ausführung von Signal {sid} fehlgeschlagen: {e}", level="ERROR")
            res.update({"executed": len(done), "failed": failed, "position_ids": done})
            if failed:
                res["_status"] = "warning"
            return done, res

    def _execute_one(self, signal_id: int, account: str) -> int | None:
        with ACCOUNT_LOCK, session_scope() as s:
            sig = s.get(Signal, signal_id)
            if sig is None or sig.status != "approved":
                return None
            if kill_switch_state(s).get("active"):
                sig.status = "vetoed"
                sig.veto_reason = "Kill Switch zwischen Freigabe und Ausführung aktiviert"
                return None
            sizing = (sig.risk_notes or {}).get("sizing") or {}
            qty = float(sizing.get("qty") or 0)
            if qty <= 0:
                sig.status = "failed"
                sig.veto_reason = "keine Positionsgröße"
                return None
            acc = get_account(s, account)
            price, pts = self.md.last_price(sig.symbol, sig.asset_class)
            if self.md.clock() - pts > self.st.stale_price_sec:
                sig.status = "failed"
                sig.veto_reason = "Kurs bei Ausführung veraltet"
                return None
            fx = self.md.eur_per(self.md.quote_ccy(sig.symbol, sig.asset_class))
            strat = STRATEGIES[sig.strategy]
            max_hold = strat.max_hold_bars * TF_SECONDS[sig.timeframe]
            cid = client_order_id(account, sig)
            reason = (sig.rationale or {}).get("text", "")
            try:
                pos = self.broker.open_position(
                    s, acc, client_order_id=cid, signal=sig, qty=qty, ref_price=price, fx=fx,
                    quote_ccy=self.md.quote_ccy(sig.symbol, sig.asset_class),
                    quote_volume=(sig.rationale or {}).get("quote_volume"), max_hold_sec=max_hold,
                    reason=reason, is_test=sig.is_test)
            except DuplicateOrder:
                sig.status = "failed"
                sig.veto_reason = f"Doppelte Order verhindert ({cid})"
                self.decision(f"Doppelte Order verhindert: {cid}", level="WARNING")
                return None
            sig.status = "executed"
            s.flush()
            self.decision(
                f"ORDER {('KAUF' if sig.side == 'long' else 'LEERVERKAUF')} {pos.qty:.6g} {sig.symbol} @ {pos.entry_price:.6g} "
                f"(Ref {price:.6g}, Slippage {pos.slippage_eur:.2f} €, Gebühr {pos.entry_fee_eur:.2f} €). "
                f"SL {pos.stop_loss:.6g}, TP {pos.take_profit:.6g}.",
                {"position_id": pos.id, "client_order_id": cid, "test": sig.is_test})
            record_equity(s, acc, force=True)
            return pos.id

    # -- monitoring -----------------------------------------------------------------
    def monitor(self, account: str) -> dict:
        with self.task("Positionen überwachen") as res:
            closed, updated, errors = [], 0, []
            with session_scope() as s:
                ks = kill_switch_state(s)
                ids = [p.id for p in s.query(Position).filter(Position.account == account,
                                                               Position.status == "open").all()]
            for pid in ids:
                try:
                    r = self._check_position(pid, account, flatten=bool(ks.get("active") and ks.get("flatten")))
                    if r == "closed":
                        closed.append(pid)
                    elif r == "updated":
                        updated += 1
                except Exception as e:  # noqa: BLE001
                    errors.append(f"Position {pid}: {e}")
                    self.log.warning("monitor position %s failed: %s", pid, e)
            with ACCOUNT_LOCK, session_scope() as s:
                acc = get_account(s, account)
                snap = record_equity(s, acc)
            res.update({"open_checked": len(ids), "closed": closed, "price_updates": updated, "errors": errors,
                        "equity": round(snap["equity"], 2), "unrealized": round(snap["unrealized"], 2)})
            if errors:
                res["_status"] = "warning"
            return res

    def _exit_fill(self, pos: Position, kind: str, ref: float, fx: float) -> Fill:
        if kind == "take_profit":
            return self.broker.limit_fill("sell" if pos.side == "long" else "buy", pos.qty, ref, pos.asset_class, fx)
        side = "sell" if pos.side == "long" else "buy"
        qv = (pos.rationale or {}).get("quote_volume")
        bps = slippage_bps(pos.asset_class, qv, pos.qty * ref * fx, self.st)
        px = ref * (1 - bps / 10_000) if side == "sell" else ref * (1 + bps / 10_000)
        return Fill(price=px, ref_price=ref, qty=pos.qty, fee_eur=fee_eur(pos.asset_class, pos.qty * px * fx, self.st),
                    slippage_bps=bps, ts=now())

    def _check_position(self, pid: int, account: str, flatten: bool) -> str:
        with ACCOUNT_LOCK, session_scope() as s:
            pos = s.get(Position, pid)
            if pos is None or pos.status != "open":
                return "skip"
            acc = get_account(s, account)
            clock = self.md.clock()
            hit: tuple[str, float, float] | None = None  # (kind, ref price, bar time)
            # 1) walk 1m bars since last check
            start = pos.last_checked_candle or (int(pos.entry_time // 60) * 60 + 60)
            try:
                bars = self.md.candles(pos.symbol, "1m", 240, asset_class=pos.asset_class, max_age=0)
                closed = bars.closed(clock)
            except Exception as e:  # noqa: BLE001
                closed = None
                self.log.debug("1m bars for %s unavailable: %s", pos.symbol, e)
            if closed is not None and len(closed):
                for i in range(len(closed)):
                    t = float(closed.t[i])
                    if t < start:
                        continue
                    hit = self._bar_hit(pos, closed.o[i], closed.h[i], closed.l[i])
                    if hit:
                        hit = (hit[0], hit[1], t)
                        break
                    pos.last_checked_candle = t + 60
            # 2) live price
            price, pts = None, None
            try:
                price, pts = self.md.last_price(pos.symbol, pos.asset_class)
            except Exception as e:  # noqa: BLE001
                self.log.debug("price for %s unavailable: %s", pos.symbol, e)
            fresh = price is not None and clock - pts <= self.st.stale_price_sec
            if price is not None:
                pos.last_price = price
                pos.last_price_time = pts
            if hit is None and fresh:
                h = self._bar_hit(pos, price, price, price)
                if h:
                    hit = (h[0], h[1], clock)
            if hit is None and fresh and clock - pos.entry_time >= pos.max_hold_sec:
                hit = ("time_exit", price, clock)
            if hit is None and fresh and flatten:
                hit = ("kill_switch", price, clock)
            if hit is None:
                return "updated" if price is not None else "skip"
            kind, ref, t = hit
            try:
                fx = self.md.eur_per(pos.quote_ccy)
            except Exception:  # noqa: BLE001
                fx = pos.fx_entry
            fill = self._exit_fill(pos, kind, ref, fx)
            labels = {"stop_loss": "Stop-Loss erreicht", "take_profit": "Take-Profit erreicht",
                      "time_exit": "Maximale Haltedauer erreicht", "kill_switch": "Kill Switch: Glattstellung"}
            rationale = {"rule": kind, "label": labels[kind], "trigger_price": ref, "bar_time": t,
                         "stop_loss": pos.stop_loss, "take_profit": pos.take_profit,
                         "held_min": round((clock - pos.entry_time) / 60, 1)}
            try:
                self.broker.close_position(s, acc, pos, fill=fill, fx=fx, reason=labels[kind], exit_rationale=rationale)
            except DuplicateOrder:
                return "skip"
            self.decision(f"GESCHLOSSEN {pos.symbol} ({pos.strategy}, {pos.side}): {labels[kind]} @ {fill.price:.6g} "
                          f"→ {pos.pnl_eur:+.2f} € ({pos.r_multiple:+.2f} R)" if pos.r_multiple is not None else
                          f"GESCHLOSSEN {pos.symbol}: {labels[kind]}",
                          {"position_id": pos.id, "pnl_eur": pos.pnl_eur, "test": pos.is_test})
            record_equity(s, acc, force=True)
            return "closed"

    @staticmethod
    def _bar_hit(pos: Position, o: float, h: float, l: float) -> tuple[str, float] | None:  # noqa: E741
        if pos.side == "long":
            if l <= pos.stop_loss:
                return "stop_loss", min(pos.stop_loss, o)
            if h >= pos.take_profit:
                return "take_profit", pos.take_profit
        else:
            if h >= pos.stop_loss:
                return "stop_loss", max(pos.stop_loss, o)
            if l <= pos.take_profit:
                return "take_profit", pos.take_profit
        return None


__all__ = ["ExecutionAgent", "time"]
