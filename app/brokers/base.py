"""Broker interface. Paper and (future) live brokers implement the same contract."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Fill:
    price: float  # executed price (incl. slippage)
    ref_price: float  # market price used as reference
    qty: float
    fee_eur: float
    slippage_bps: float
    ts: float


class Broker:
    name = "base"
    is_live = False

    def market_fill(self, symbol: str, side: str, qty: float, ref_price: float, asset_class: str,
                    fx: float, quote_volume: float | None) -> Fill:  # pragma: no cover - interface
        raise NotImplementedError
