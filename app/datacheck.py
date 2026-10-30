"""Live market data check: `python -m app.datacheck` (exit 0 = real data arrives and is fresh).

Run this on the server - it proves that real prices are loaded (test 1 of the
mandatory checks) independently of the trading pipeline.
"""
from __future__ import annotations

import sys
import time

from app.config import get_settings


def main() -> int:
    st = get_settings()
    ok = True
    print(f"Marktdaten-Check {time.strftime('%Y-%m-%d %H:%M:%S')}")
    if st.enable_crypto:
        from app.data.binance import BinanceProvider

        b = BinanceProvider()
        try:
            t0 = time.time()
            tick = b.list_tickers()
            liquid = [t for t in tick if t.quote_volume >= st.crypto_min_quote_volume]
            btc = next((t for t in tick if t.symbol == "BTCUSDT"), None)
            print(f"  Binance Ticker: {len(tick)} USDT-Märkte ({len(liquid)} liquide) in {time.time() - t0:.1f}s "
                  f"via {b.hosts[b._preferred]}; BTCUSDT={btc.price if btc else '–'}")
            c = b.candles("BTCUSDT", "1m", 5)
            age = time.time() - c.t[-1]
            print(f"  Binance 1m-Kerzen: letzte Kerze vor {age:.0f}s, Schluss {c.c[-1]}")
            ok &= age < 180 and len(liquid) > 10
        except Exception as e:  # noqa: BLE001
            print(f"  Binance FEHLER: {e}")
            ok = False
    if st.enable_stocks:
        from app.data.yahoo import YahooProvider

        y = YahooProvider(universe=st.stock_universe[:3])
        try:
            for sym in st.stock_universe[:3]:
                c = y.candles(sym, "1d", 5)
                print(f"  Yahoo {sym}: {len(c)} Tageskerzen, Schluss {c.c[-1]}, Währung {y.quote_ccy(sym)}, "
                      f"Markt offen: {y.is_market_open(sym)}")
        except Exception as e:  # noqa: BLE001
            print(f"  Yahoo FEHLER: {e}")
            ok = False
    from app.data.fx import FxService

    try:
        rate = FxService(binance_hosts=st.binance_hosts).eur_per("USD")
        print(f"  FX: 1 USD = {rate:.4f} EUR")
    except Exception as e:  # noqa: BLE001
        print(f"  FX FEHLER: {e}")
        ok = False
    print("ERGEBNIS:", "OK – echte Marktdaten werden geladen" if ok else "FEHLER")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
