"""Market data providers with mocked HTTP (response formats of the public APIs)."""
import json
import time

import pytest
import requests

from app.data.base import Candles, DataError
from app.data.binance import BinanceProvider
from app.data.http import CircuitOpen, HttpError, ResilientHttp
from app.data.yahoo import YahooProvider


class Resp:
    def __init__(self, status, body, headers=None):
        self.status_code = status
        self._body = body
        self.headers = headers or {}
        self.text = json.dumps(body) if not isinstance(body, str) else body

    def json(self):
        if isinstance(self._body, str):
            raise ValueError("no json")
        return self._body


def http_with(handler):
    h = ResilientHttp(max_retries=2, backoff_base=0.001)
    h.session.get = lambda url, params=None, timeout=None, headers=None: handler(url, params or {})
    return h


NOW_MS = int(time.time() * 1000)
TICKERS = [
    {"symbol": "BTCUSDT", "lastPrice": "65000.5", "priceChangePercent": "2.5", "quoteVolume": "900000000",
     "highPrice": "66000", "lowPrice": "63000", "closeTime": NOW_MS, "count": 100000},
    {"symbol": "USDCUSDT", "lastPrice": "1.0", "priceChangePercent": "0", "quoteVolume": "500000000",
     "highPrice": "1", "lowPrice": "1", "closeTime": NOW_MS, "count": 1000},
    {"symbol": "BTCUPUSDT", "lastPrice": "5", "priceChangePercent": "1", "quoteVolume": "9000000",
     "highPrice": "5", "lowPrice": "5", "closeTime": NOW_MS, "count": 10},
    {"symbol": "OLDUSDT", "lastPrice": "5", "priceChangePercent": "1", "quoteVolume": "9000000",
     "highPrice": "5", "lowPrice": "5", "closeTime": NOW_MS - 86400_000 * 3, "count": 0},
    {"symbol": "ETHBTC", "lastPrice": "0.05", "priceChangePercent": "1", "quoteVolume": "900",
     "highPrice": "5", "lowPrice": "5", "closeTime": NOW_MS, "count": 10},
]
KLINES = [[NOW_MS - 120_000, "100", "101", "99", "100.5", "10", NOW_MS - 60_001, "1005", 50, "5", "500", "0"],
          [NOW_MS - 60_000, "100.5", "102", "100", "101.5", "12", NOW_MS + 0, "1218", 60, "6", "600", "0"]]


def test_binance_tickers_filter_and_parse():
    http = http_with(lambda url, p: Resp(200, TICKERS))
    t = BinanceProvider(http=http, hosts=["https://api.binance.com"]).list_tickers()
    assert [x.symbol for x in t] == ["BTCUSDT"]  # stablecoin, leveraged token, delisted, non-USDT removed
    assert t[0].price == 65000.5 and t[0].quote_volume == 9e8 and t[0].change_pct == 2.5


def test_binance_klines_parse_and_closed_bar_logic():
    http = http_with(lambda url, p: Resp(200, KLINES))
    c = BinanceProvider(http=http, hosts=["https://x"]).candles("BTCUSDT", "1m", 2)
    assert len(c) == 2 and c.c[-1] == 101.5 and c.v[0] == 1005.0  # volume = quote volume
    assert len(c.closed(now=(NOW_MS - 30_000) / 1000)) == 1  # forming bar dropped


def test_binance_failover_on_geoblock():
    calls = []

    def handler(url, p):
        calls.append(url)
        if "api.binance.com" in url:
            return Resp(451, {"code": 0, "msg": "Service unavailable from a restricted location"})
        return Resp(200, {"symbol": "BTCUSDT", "price": "65000.0"})

    prov = BinanceProvider(http=http_with(handler), hosts=["https://api.binance.com", "https://data-api.binance.vision"])
    price, _ = prov.last_price("BTCUSDT")
    assert price == 65000.0 and "data-api.binance.vision" in calls[-1]
    prov.last_price("BTCUSDT")
    assert "data-api.binance.vision" in calls[-1] and len(calls) == 3  # remembers working host


def test_retry_then_success_and_rate_limit_handling():
    seq = [requests.ConnectionError("boom"), Resp(503, {}), Resp(200, {"ok": 1})]

    def handler(url, p):
        r = seq.pop(0)
        if isinstance(r, Exception):
            raise r
        return r

    h = http_with(handler)
    assert h.get_json("https://example.org/x") == {"ok": 1}
    assert h.stats["example.org"].failures == 2 and h.stats["example.org"].consecutive_failures == 0

    h2 = http_with(lambda url, p: Resp(429, {}, {"Retry-After": "30"}))
    with pytest.raises(HttpError):
        h2.get_json("https://rl.org/x")
    with pytest.raises(CircuitOpen):  # host blocked until Retry-After passes
        h2.get_json("https://rl.org/x")


def test_circuit_breaker_opens_after_repeated_failures():
    def handler(url, p):
        raise requests.Timeout("t")

    h = http_with(handler)
    for _ in range(3):
        with pytest.raises(HttpError):
            h.get_json("https://down.org/x")
    assert h.snapshot()["down.org"]["circuit_open"]


def yahoo_chart(ts, closes, currency="USD", start=None, end=None):
    n = len(ts)
    return {"chart": {"result": [{
        "meta": {"currency": currency, "regularMarketPrice": closes[-1], "regularMarketTime": int(time.time()) - 30,
                 "chartPreviousClose": closes[0], "exchangeName": "NMS",
                 "currentTradingPeriod": {"regular": {"start": start or int(time.time()) - 3600,
                                                     "end": end or int(time.time()) + 3600}}},
        "timestamp": ts,
        "indicators": {"quote": [{"open": closes, "high": [c * 1.01 for c in closes], "low": [c * 0.99 for c in closes],
                                  "close": closes[:-2] + [None] + closes[-1:], "volume": [1000] * n}]},
    }], "error": None}}


def test_yahoo_parse_drops_null_bars_and_detects_currency():
    now = int(time.time())
    ts = [now - 86400 * (30 - i) for i in range(30)]
    closes = [100.0 + i for i in range(30)]
    y = YahooProvider(http=http_with(lambda url, p: Resp(200, yahoo_chart(ts, closes, "EUR"))), universe=["SAP.DE"])
    c = y.candles("SAP.DE", "1d", 100)
    assert len(c) == 29  # one null close removed
    assert y.quote_ccy("SAP.DE") == "EUR"
    tick = y.list_tickers()[0]
    assert tick.price == closes[-1] and tick.market_open


def test_yahoo_errors_raise_data_error():
    y = YahooProvider(http=http_with(lambda url, p: Resp(404, {"chart": {"result": None, "error": "Not Found"}})),
                      universe=["NOPE"])
    with pytest.raises(DataError):
        y.candles("NOPE", "1d")
    with pytest.raises(DataError):
        y.list_tickers()


def test_stale_detection():
    t = time.time()
    fresh = Candles.from_rows("X", "1m", [(t - 60, 1, 1, 1, 1, 1)])
    old = Candles.from_rows("X", "1m", [(t - 3600, 1, 1, 1, 1, 1)])
    assert not fresh.is_stale(t) and old.is_stale(t)
