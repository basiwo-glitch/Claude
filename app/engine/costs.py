"""Transaction cost model: fees + slippage (both always charged against P&L)."""
from __future__ import annotations

from app.config import Settings, get_settings


def fee_eur(asset_class: str, notional_eur: float, st: Settings | None = None) -> float:
    st = st or get_settings()
    if asset_class == "stock":
        return notional_eur * st.stock_fee_pct / 100 + st.stock_fee_fixed_eur
    return notional_eur * st.crypto_fee_pct / 100


def slippage_bps(asset_class: str, quote_volume_24h: float | None, notional_eur: float,
                 st: Settings | None = None) -> float:
    """Base spread/impact + liquidity penalty + size relative to traded volume."""
    st = st or get_settings()
    base = st.stock_slippage_bps if asset_class == "stock" else st.crypto_slippage_bps
    qv = quote_volume_24h or 0.0
    mult = 1.0
    if asset_class == "crypto":
        if qv < 20e6:
            mult = 3.0
        elif qv < 100e6:
            mult = 1.5
    else:
        if qv < 50e6:
            mult = 2.0
    impact = min(50.0, 2000.0 * notional_eur / qv) if qv > 0 else 25.0
    return base * mult + impact


def round_trip_cost_frac(asset_class: str, quote_volume_24h: float | None, est_notional_eur: float,
                         st: Settings | None = None) -> float:
    """Costs of entry + exit as a fraction of price (used in the probability model)."""
    st = st or get_settings()
    fees = 2 * fee_eur(asset_class, est_notional_eur, st) / max(est_notional_eur, 1.0)
    slip = 2 * slippage_bps(asset_class, quote_volume_24h, est_notional_eur, st) / 10_000
    return fees + slip
