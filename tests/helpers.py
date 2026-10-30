import numpy as np

from app import clock
from app.data.base import Candles, MarketDataProvider, Ticker
from app.data.fx import FxService
from app.data.hub import MarketData
from app.db import Signal, session_scope


class FakeProvider(MarketDataProvider):
    name = "fake"
    asset_class = "crypto"

    def __init__(self):
        self.prices: dict[str, tuple[float, float]] = {}
        self.bars: dict[str, list[tuple]] = {}
        self.open = True

    def set_price(self, sym, p, t=None):
        self.prices[sym] = (p, t if t is not None else clock.now())

    def add_bar(self, sym, t, o, h, l, c):  # noqa: E741
        self.bars.setdefault(sym, []).append((t, o, h, l, c, 1000.0))

    def last_price(self, symbol):
        return self.prices[symbol]

    def candles(self, symbol, tf, limit=500):
        rows = self.bars.get(symbol, [])
        return Candles.from_rows(symbol, tf, rows[-limit:], "fake")

    def list_tickers(self):
        return [Ticker(s, "crypto", p, 0.0, 1e9, p, p, "USD", t) for s, (p, t) in self.prices.items()]

    def is_market_open(self, symbol, now=None):
        return self.open


def fake_md(fx=0.9):
    p = FakeProvider()
    md = MarketData(providers={"crypto": p}, fx=FxService(fixed={"USD": fx}), synthetic=True)
    return md, p


def make_signal(symbol="AAAUSDT", side="long", entry=100.0, sl=98.0, tp=104.0, strategy="breakout", n=50,
                ev=0.3, account="PAPER", atr=1.0, bar_time=1000.0, status="proposed", is_test=False) -> int:
    with session_scope() as s:
        from app.db import Cycle

        c = Cycle(account=account, status="ok", steps={})
        s.add(c)
        s.flush()
        g = Signal(cycle_id=c.id, account=account, symbol=symbol, asset_class="crypto", strategy=strategy, side=side,
                   timeframe="1h", bar_time=bar_time, entry=entry, stop_loss=sl, take_profit=tp, probability=0.5,
                   sample_size=n, expected_value_r=ev, reward_risk=abs(tp - entry) / abs(entry - sl), cost_r=0.05,
                   rationale={"indicators": {"atr": atr}, "quote_volume": 1e9, "text": "test"}, status=status,
                   is_test=is_test)
        s.add(g)
        s.flush()
        return g.id


def arr(x):
    return np.array(x, dtype=float)
