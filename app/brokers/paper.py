"""Paper broker: simulated fills on real prices with fees and adverse slippage."""
from __future__ import annotations

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.brokers.base import Broker, Fill
from app.config import get_settings
from app.db import Account, Order, Position, now
from app.engine.costs import fee_eur, slippage_bps


class DuplicateOrder(Exception):
    pass


class PaperBroker(Broker):
    name = "paper"
    is_live = False

    def market_fill(self, symbol, side, qty, ref_price, asset_class, fx, quote_volume) -> Fill:
        notional = qty * ref_price * fx
        bps = slippage_bps(asset_class, quote_volume, notional, get_settings())
        px = ref_price * (1 + bps / 10_000) if side == "buy" else ref_price * (1 - bps / 10_000)
        return Fill(price=px, ref_price=ref_price, qty=qty, fee_eur=fee_eur(asset_class, qty * px * fx),
                    slippage_bps=bps, ts=now())

    def limit_fill(self, side, qty, limit_price, asset_class, fx) -> Fill:
        """Take-profit limit order: filled at the limit price, no slippage."""
        return Fill(price=limit_price, ref_price=limit_price, qty=qty,
                    fee_eur=fee_eur(asset_class, qty * limit_price * fx), slippage_bps=0.0, ts=now())

    # ------------------------------------------------------------------------------
    def open_position(self, s: Session, acc: Account, *, client_order_id: str, signal, qty: float, ref_price: float,
                      fx: float, quote_ccy: str, quote_volume: float | None, max_hold_sec: int, reason: str,
                      is_test: bool) -> Position:
        if s.query(Order.id).filter(Order.client_order_id == client_order_id).first():
            raise DuplicateOrder(client_order_id)
        side_order = "buy" if signal.side == "long" else "sell"
        f = self.market_fill(signal.symbol, side_order, qty, ref_price, signal.asset_class, fx, quote_volume)
        notional = qty * f.price * fx
        risk_eur = qty * abs(f.price - signal.stop_loss) * fx
        pos = Position(
            account=acc.name, client_order_id=client_order_id, signal_id=signal.id, symbol=signal.symbol,
            asset_class=signal.asset_class, strategy=signal.strategy, side=signal.side, timeframe=signal.timeframe,
            qty=qty, quote_ccy=quote_ccy, fx_entry=fx, entry_price=f.price, entry_ref_price=f.ref_price,
            entry_time=f.ts, stop_loss=signal.stop_loss, take_profit=signal.take_profit, initial_risk_eur=risk_eur,
            entry_fee_eur=f.fee_eur, max_hold_sec=max_hold_sec, status="open", last_price=f.ref_price,
            last_price_time=f.ts, slippage_eur=qty * abs(f.price - f.ref_price) * fx,
            rationale=signal.rationale, is_test=is_test,
        )
        try:
            with s.begin_nested():
                s.add(pos)
                s.flush()
                s.add(Order(account=acc.name, client_order_id=client_order_id, position_id=pos.id,
                            created_at=f.ts, symbol=signal.symbol, side=side_order, kind="entry", qty=qty,
                            ref_price=f.ref_price, fill_price=f.price, fee_eur=f.fee_eur,
                            slippage_bps=f.slippage_bps, broker=self.name, reason=reason, is_test=is_test))
                s.flush()
        except IntegrityError as e:
            raise DuplicateOrder(client_order_id) from e
        if signal.side == "long":
            acc.cash -= notional + f.fee_eur
        else:
            acc.cash += notional - f.fee_eur
        return pos

    def close_position(self, s: Session, acc: Account, pos: Position, *, fill: Fill, fx: float, reason: str,
                       exit_rationale: dict) -> Position:
        cid = f"{pos.client_order_id}:exit"
        if s.query(Order.id).filter(Order.client_order_id == cid).first() or pos.status != "open":
            raise DuplicateOrder(cid)
        side_order = "sell" if pos.side == "long" else "buy"
        notional = pos.qty * fill.price * fx
        s.add(Order(account=acc.name, client_order_id=cid, position_id=pos.id, created_at=fill.ts,
                    symbol=pos.symbol, side=side_order, kind="exit", qty=pos.qty, ref_price=fill.ref_price,
                    fill_price=fill.price, fee_eur=fill.fee_eur, slippage_bps=fill.slippage_bps, broker=self.name,
                    reason=reason, is_test=pos.is_test))
        if pos.side == "long":
            acc.cash += notional - fill.fee_eur
            gross = pos.qty * (fill.price * fx - pos.entry_price * pos.fx_entry)
        else:
            acc.cash -= notional + fill.fee_eur
            gross = pos.qty * (pos.entry_price * pos.fx_entry - fill.price * fx)
        fees = pos.entry_fee_eur + fill.fee_eur
        pos.status = "closed"
        pos.exit_price = fill.price
        pos.exit_ref_price = fill.ref_price
        pos.exit_time = fill.ts
        pos.exit_reason = reason
        pos.exit_fee_eur = fill.fee_eur
        pos.fx_exit = fx
        pos.gross_pnl_eur = gross
        pos.pnl_eur = gross - fees
        entry_notional = pos.qty * pos.entry_price * pos.fx_entry
        pos.pnl_pct = pos.pnl_eur / entry_notional * 100 if entry_notional else 0.0
        pos.r_multiple = pos.pnl_eur / pos.initial_risk_eur if pos.initial_risk_eur else None
        pos.slippage_eur = (pos.slippage_eur or 0) + pos.qty * abs(fill.price - fill.ref_price) * fx
        pos.last_price = fill.ref_price
        pos.last_price_time = fill.ts
        pos.exit_rationale = exit_rationale
        return pos
