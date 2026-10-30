from app.strategies.base import Strategy
from app.strategies.library import Breakout, DayTrading, MeanReversion, Momentum, Scalping, TrendFollowing

STRATEGIES: dict[str, Strategy] = {s.name: s for s in
                                   (Scalping(), DayTrading(), Momentum(), TrendFollowing(), Breakout(), MeanReversion())}

__all__ = ["STRATEGIES", "Strategy"]
