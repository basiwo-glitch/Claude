"""The six rule-based strategies. Every rule is explicit and testable.

Short rules mirror long rules. Whether a strategy is any good is NOT assumed:
the Strategy Analyst measures it on history, the Risk Manager demands positive
expected value after costs, and the Performance Analyst disables strategies whose
forward-test results are bad.
"""
from __future__ import annotations

import numpy as np

from app.indicators import Indicators
from app.strategies.base import Condition, Strategy, cross_down, cross_up, shift


class Scalping(Strategy):
    name = "scalping"
    label = "Scalping"
    description = ("5-Minuten-Pullback im Mikro-Trend: EMA9 über EMA21, Kurs über EMA50, RSI kreuzt 50 "
                   "nach Rücksetzer, Volumen über Schnitt. Enges Ziel, enger Stop.")
    timeframes = {"crypto": "5m", "stock": "5m"}
    max_hold_bars = 12
    sl_atr = 1.0
    tp_atr = 1.5
    warmup = 60

    def conditions(self, ind: Indicators) -> list[Condition]:
        return [
            Condition("ema_stack", "EMA9 vs. EMA21", ind.ema9 > ind.ema21, ind.ema9 < ind.ema21,
                      (ind.ema9 / ind.ema21 - 1) * 100, "{:+.3f} %"),
            Condition("trend", "Kurs vs. EMA50", ind.c > ind.ema50, ind.c < ind.ema50,
                      (ind.c / ind.ema50 - 1) * 100, "{:+.2f} %"),
            Condition("rsi_cross", "RSI kreuzt 50", cross_up(ind.rsi, 50.0), cross_down(ind.rsi, 50.0), ind.rsi),
            Condition("volume", "Volumen > Ø20", ind.vol_ratio > 1.0, ind.vol_ratio > 1.0, ind.vol_ratio, "{:.2f}x"),
        ]


class DayTrading(Strategy):
    name = "daytrading"
    label = "Daytrading"
    description = ("15-Minuten: EMA20 über EMA50, MACD-Histogramm dreht über null, RSI zwischen 50 und 70 "
                   "(short gespiegelt). Haltedauer max. 8 Stunden.")
    timeframes = {"crypto": "15m", "stock": "15m"}
    max_hold_bars = 32
    sl_atr = 1.5
    tp_atr = 2.5
    warmup = 60

    def conditions(self, ind: Indicators) -> list[Condition]:
        return [
            Condition("ema_trend", "EMA20 vs. EMA50", ind.ema20 > ind.ema50, ind.ema20 < ind.ema50,
                      (ind.ema20 / ind.ema50 - 1) * 100, "{:+.2f} %"),
            Condition("macd_turn", "MACD-Histogramm kreuzt 0", cross_up(ind.macd_hist, 0.0),
                      cross_down(ind.macd_hist, 0.0), ind.macd_hist, "{:.5g}"),
            Condition("rsi_zone", "RSI-Zone 50–70 / 30–50", (ind.rsi > 50) & (ind.rsi < 70),
                      (ind.rsi < 50) & (ind.rsi > 30), ind.rsi),
        ]


class Momentum(Strategy):
    name = "momentum"
    label = "Momentum"
    description = ("1-Stunde: starke 12-Bar-Rendite (> 1,5 ATR), neues 10-Bar-Hoch, Volumen ≥ 1,5x, "
                   "RSI 55–80, Kurs über EMA50.")
    timeframes = {"crypto": "1h", "stock": "1h"}
    max_hold_bars = 24
    sl_atr = 2.0
    tp_atr = 3.0
    warmup = 60

    def conditions(self, ind: Indicators) -> list[Condition]:
        thr = 1.5 * ind.atr_pct
        return [
            Condition("roc", "ROC12 vs. 1,5×ATR%", ind.roc12 > thr, ind.roc12 < -thr, ind.roc12, "{:+.2f} %"),
            Condition("new_extreme", "Neues 10-Bar-Extrem", ind.h >= ind.hh10, ind.l <= ind.ll10),
            Condition("volume", "Volumen ≥ 1,5× Ø20", ind.vol_ratio >= 1.5, ind.vol_ratio >= 1.5, ind.vol_ratio,
                      "{:.2f}x"),
            Condition("rsi", "RSI 55–80 / 20–45", (ind.rsi > 55) & (ind.rsi < 80), (ind.rsi < 45) & (ind.rsi > 20),
                      ind.rsi),
            Condition("trend", "Kurs vs. EMA50", ind.c > ind.ema50, ind.c < ind.ema50,
                      (ind.c / ind.ema50 - 1) * 100, "{:+.2f} %"),
        ]


class TrendFollowing(Strategy):
    name = "trend"
    label = "Trend Following"
    description = ("4h (Aktien: 1d): EMA50 über EMA200, ADX > 20, Rücksetzer an die EMA20 in den letzten "
                   "3 Kerzen, Schluss wieder über EMA20 mit grüner Kerze.")
    timeframes = {"crypto": "4h", "stock": "1d"}
    max_hold_bars = 30
    sl_atr = 2.0
    tp_atr = 4.0
    warmup = 210

    def conditions(self, ind: Indicators) -> list[Condition]:
        touched_l = (ind.l <= ind.ema20) | (shift(ind.l, 1) <= shift(ind.ema20, 1)) | (shift(ind.l, 2) <= shift(ind.ema20, 2))
        touched_s = (ind.h >= ind.ema20) | (shift(ind.h, 1) >= shift(ind.ema20, 1)) | (shift(ind.h, 2) >= shift(ind.ema20, 2))
        return [
            Condition("regime", "EMA50 vs. EMA200", ind.ema50 > ind.ema200, ind.ema50 < ind.ema200,
                      (ind.ema50 / ind.ema200 - 1) * 100, "{:+.2f} %"),
            Condition("adx", "ADX > 20", ind.adx > 20, ind.adx > 20, ind.adx, "{:.1f}"),
            Condition("pullback", "Rücksetzer an EMA20 (3 Kerzen)", touched_l, touched_s),
            Condition("resume", "Schluss jenseits EMA20 + Kerzenfarbe", (ind.c > ind.ema20) & (ind.c > ind.o),
                      (ind.c < ind.ema20) & (ind.c < ind.o)),
        ]


class Breakout(Strategy):
    name = "breakout"
    label = "Breakout"
    description = ("1-Stunde: Schluss über dem 20-Bar-Hoch (unter dem 20-Bar-Tief) nach Bollinger-Squeeze "
                   "(Bandbreite unter 50-Bar-Median) mit Volumen ≥ 1,5x.")
    timeframes = {"crypto": "1h", "stock": "1h"}
    max_hold_bars = 24
    sl_atr = 1.5
    tp_atr = 3.0
    warmup = 80

    def conditions(self, ind: Indicators) -> list[Condition]:
        squeeze = shift(ind.bb_width, 1) < shift(ind.bb_width_med50, 1)
        return [
            Condition("break", "Schluss jenseits 20-Bar-Hoch/Tief", ind.c > ind.hh20_prev, ind.c < ind.ll20_prev,
                      ind.c, "{:.6g}"),
            Condition("squeeze", "Vorherige Kompression (BB-Breite < Median50)", squeeze, squeeze,
                      shift(ind.bb_width, 1) * 100, "{:.2f} %"),
            Condition("volume", "Volumen ≥ 1,5× Ø20", ind.vol_ratio >= 1.5, ind.vol_ratio >= 1.5, ind.vol_ratio,
                      "{:.2f}x"),
        ]


class MeanReversion(Strategy):
    name = "mean_reversion"
    label = "Mean Reversion"
    description = ("15-Minuten: Schluss außerhalb des unteren (oberen) Bollinger-Bands, RSI < 30 (> 70), "
                   "ADX < 25 (kein starker Trend). Ziel: Bollinger-Mitte.")
    timeframes = {"crypto": "15m", "stock": "15m"}
    max_hold_bars = 16
    sl_atr = 1.25
    min_rr = 0.8  # high-hit-rate style; the EV check decides
    warmup = 60

    def conditions(self, ind: Indicators) -> list[Condition]:
        return [
            Condition("band", "Schluss außerhalb Bollinger-Band", ind.c < ind.bb_lower, ind.c > ind.bb_upper,
                      ind.bb_pctb, "%B {:.2f}"),
            Condition("rsi", "RSI < 30 / > 70", ind.rsi < 30, ind.rsi > 70, ind.rsi),
            Condition("range", "ADX < 25", ind.adx < 25, ind.adx < 25, ind.adx, "{:.1f}"),
        ]

    def levels(self, ind: Indicators, side: str):
        a = ind.atr
        if side == "long":
            return ind.c - self.sl_atr * a, np.maximum(ind.bb_mid, ind.c + 0.25 * a)
        return ind.c + self.sl_atr * a, np.minimum(ind.bb_mid, ind.c - 0.25 * a)
