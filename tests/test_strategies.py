"""Strategy rules and the historical replay used as probability model."""
import numpy as np

from app.data.base import Candles
from app.data.synthetic import SyntheticProvider
from app.indicators import compute
from app.strategies import STRATEGIES
from app.strategies.base import Strategy, wilson


class FixedLevels(Strategy):
    """Test strategy: enters on given bars with fixed SL/TP distances."""
    name = "fixed"
    max_hold_bars = 5
    warmup = 0

    def __init__(self, bars, sl=1.0, tp=2.0):
        self.bars, self.sl, self.tp = bars, sl, tp

    def conditions(self, ind):
        return []

    def entries(self, ind, conds=None):
        e = np.zeros(len(ind), dtype=bool)
        e[self.bars] = True
        return e, np.zeros(len(ind), dtype=bool)

    def levels(self, ind, side):
        return ind.c - self.sl, ind.c + self.tp


def mk(o, h, l, c):
    n = len(c)
    return compute(Candles("T", "1h", np.arange(n) * 3600.0, np.array(o, float), np.array(h, float),
                           np.array(l, float), np.array(c, float), np.ones(n)))


def test_replay_take_profit_and_stop_first_rule():
    # bar0 entry @100 (SL 99, TP 102). bar1 touches both 98.5 and 102.5 -> stop assumed first
    ind = mk([100, 100], [100, 102.5], [100, 98.5], [100, 101])
    s = FixedLevels([0]).replay(ind, "long", 0.0)
    assert s.n == 1 and s.sl_hits == 1 and abs(s.r_values[0] - (-1.0)) < 1e-9


def test_replay_gap_through_stop_fills_at_open():
    ind = mk([100, 97], [100, 97.5], [100, 96], [100, 97])
    s = FixedLevels([0]).replay(ind, "long", 0.0)
    assert abs(s.r_values[0] - (-3.0)) < 1e-9  # filled at open 97, risk 1


def test_replay_tp_costs_and_time_exit():
    ind = mk([100, 100, 101], [100, 101, 102.2], [100, 99.5, 100.5], [100, 100.5, 102])
    s = FixedLevels([0]).replay(ind, "long", cost_frac=0.002)
    assert s.tp_hits == 1 and abs(s.r_values[0] - (2.0 - 0.2)) < 1e-9  # 0.2 % of 100 = 0.2 = 0.2 R
    # time exit after 5 bars at close
    c = [100, 100.2, 100.1, 100.3, 100.2, 100.4, 100.6]
    ind = mk(c, [x + 0.1 for x in c], [x - 0.1 for x in c], c)
    s = FixedLevels([0]).replay(ind, "long", 0.0)
    assert s.time_exits == 1 and abs(s.r_values[0] - 0.4) < 1e-9


def test_replay_ignores_unresolved_and_overlapping_trades():
    c = [100, 100.2, 100.1, 100.3]
    ind = mk(c, [x + 0.1 for x in c], [x - 0.1 for x in c], c)
    s = FixedLevels([0, 1, 2]).replay(ind, "long", 0.0)
    assert s.n == 0  # nothing resolved within the data -> no look-ahead, no invented outcome


def test_wilson_interval():
    lo, hi = wilson(50, 100)
    assert 0.40 < lo < 0.41 and 0.59 < hi < 0.60
    assert wilson(0, 0) == (0.0, 1.0)


def test_all_strategies_run_on_data_and_explain_conditions():
    p = SyntheticProvider(symbols=["SYNBTC"])
    for name, st in STRATEGIES.items():
        ind = compute(p.candles("SYNBTC", st.timeframe("crypto"), 1000).closed())
        conds = st.conditions(ind)
        lo, sh = st.entries(ind, conds)
        assert lo.shape == sh.shape == (len(ind),)
        assert not (lo[: st.warmup].any() or sh[: st.warmup].any())
        ex = st.explain(ind, len(ind) - 1, "long", conds)
        assert len(ex) == len(conds) and all("met" in e for e in ex)
        sl, tp = st.levels(ind, "long")
        i = len(ind) - 1
        assert sl[i] < ind.c[i] < tp[i]
        sl, tp = st.levels(ind, "short")
        assert tp[i] < ind.c[i] < sl[i]
        stats = st.replay(ind, "long", 0.002)
        assert stats.n == len(stats.r_values)
