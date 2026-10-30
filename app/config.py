"""Central configuration.

All values come from environment variables (or a local .env file).
Secrets (API keys, passwords) are only ever read from the environment and are
never written to the database or returned by the API.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from functools import lru_cache

try:  # optional, only used for local development
    from dotenv import load_dotenv

    load_dotenv()
except Exception:  # pragma: no cover
    pass


def _env(name: str, default: str | None = None) -> str | None:
    v = os.getenv(name)
    return v if v not in (None, "") else default


def _bool(name: str, default: bool) -> bool:
    v = _env(name)
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "on", "ja")


def _float(name: str, default: float) -> float:
    v = _env(name)
    return float(v) if v is not None else default


def _int(name: str, default: int) -> int:
    v = _env(name)
    return int(v) if v is not None else default


def _list(name: str, default: str) -> list[str]:
    v = _env(name, default) or ""
    return [x.strip() for x in v.split(",") if x.strip()]


DEFAULT_STOCKS = (
    "AAPL,MSFT,NVDA,AMZN,GOOGL,META,TSLA,AVGO,AMD,NFLX,ADBE,CRM,ORCL,INTC,QCOM,"
    "CSCO,PEP,KO,COST,WMT,JPM,BAC,GS,V,MA,XOM,CVX,UNH,LLY,PFE,MRK,JNJ,DIS,BA,CAT,"
    "UBER,SHOP,PLTR,COIN,MSTR,SMCI,MU,ARM,PYPL,SQ,ABNB,SPY,QQQ,IWM,"
    "SAP.DE,SIE.DE,ALV.DE,DTE.DE,BAS.DE,BMW.DE,MBG.DE,VOW3.DE,ADS.DE,IFX.DE,RHM.DE,DBK.DE"
)


@dataclass(frozen=True)
class Settings:
    # --- general -----------------------------------------------------------
    env: str = field(default_factory=lambda: _env("APP_ENV", "production"))
    database_url: str = field(default_factory=lambda: _env("DATABASE_URL", "sqlite:///./data/trader.db"))
    log_level: str = field(default_factory=lambda: _env("LOG_LEVEL", "INFO"))
    log_dir: str = field(default_factory=lambda: _env("LOG_DIR", "./data/logs"))

    # --- dashboard auth ------------------------------------------------------
    dashboard_user: str = field(default_factory=lambda: _env("DASHBOARD_USER", "admin"))
    dashboard_password: str | None = field(default_factory=lambda: _env("DASHBOARD_PASSWORD"))

    # --- market data ---------------------------------------------------------
    # "live" = real providers (Binance + Yahoo). "synthetic" = test data only;
    # in synthetic mode every trade is booked on the TEST account and flagged.
    data_mode: str = field(default_factory=lambda: _env("MARKET_DATA_MODE", "live"))
    enable_crypto: bool = field(default_factory=lambda: _bool("ENABLE_CRYPTO", True))
    enable_stocks: bool = field(default_factory=lambda: _bool("ENABLE_STOCKS", True))
    crypto_quote: str = field(default_factory=lambda: _env("CRYPTO_QUOTE", "USDT"))
    crypto_min_quote_volume: float = field(default_factory=lambda: _float("CRYPTO_MIN_QUOTE_VOLUME", 5_000_000))
    max_markets_per_cycle: int = field(default_factory=lambda: _int("MAX_MARKETS_PER_CYCLE", 1000))
    stock_universe: list[str] = field(default_factory=lambda: _list("STOCK_UNIVERSE", DEFAULT_STOCKS))
    binance_hosts: list[str] = field(
        default_factory=lambda: _list(
            "BINANCE_HOSTS",
            "https://api.binance.com,https://api1.binance.com,https://api2.binance.com,"
            "https://api3.binance.com,https://data-api.binance.vision",
        )
    )

    # --- scheduling -----------------------------------------------------------
    cycle_interval_sec: int = field(default_factory=lambda: _int("CYCLE_INTERVAL_SEC", 300))
    monitor_interval_sec: int = field(default_factory=lambda: _int("MONITOR_INTERVAL_SEC", 30))
    performance_interval_sec: int = field(default_factory=lambda: _int("PERFORMANCE_INTERVAL_SEC", 900))
    deep_scan_top_n: int = field(default_factory=lambda: _int("DEEP_SCAN_TOP_N", 40))
    analysis_top_n: int = field(default_factory=lambda: _int("ANALYSIS_TOP_N", 12))
    cycle_timeout_sec: int = field(default_factory=lambda: _int("CYCLE_TIMEOUT_SEC", 240))
    stale_price_sec: int = field(default_factory=lambda: _int("STALE_PRICE_SEC", 180))

    # --- paper account -------------------------------------------------------
    account_currency: str = "EUR"
    starting_capital: float = field(default_factory=lambda: _float("STARTING_CAPITAL_EUR", 10_000.0))
    allow_shorts: bool = field(default_factory=lambda: _bool("ALLOW_SHORTS", True))

    # --- costs ----------------------------------------------------------------
    crypto_fee_pct: float = field(default_factory=lambda: _float("CRYPTO_FEE_PCT", 0.10))  # Binance taker
    stock_fee_pct: float = field(default_factory=lambda: _float("STOCK_FEE_PCT", 0.0))
    stock_fee_fixed_eur: float = field(default_factory=lambda: _float("STOCK_FEE_FIXED_EUR", 1.0))
    crypto_slippage_bps: float = field(default_factory=lambda: _float("CRYPTO_SLIPPAGE_BPS", 3.0))
    stock_slippage_bps: float = field(default_factory=lambda: _float("STOCK_SLIPPAGE_BPS", 4.0))

    # --- risk -----------------------------------------------------------------
    risk_per_trade_pct: float = field(default_factory=lambda: _float("RISK_PER_TRADE_PCT", 0.5))
    max_position_pct: float = field(default_factory=lambda: _float("MAX_POSITION_PCT", 15.0))
    max_gross_exposure_pct: float = field(default_factory=lambda: _float("MAX_GROSS_EXPOSURE_PCT", 80.0))
    max_open_positions: int = field(default_factory=lambda: _int("MAX_OPEN_POSITIONS", 8))
    max_daily_loss_pct: float = field(default_factory=lambda: _float("MAX_DAILY_LOSS_PCT", 3.0))
    max_drawdown_pct: float = field(default_factory=lambda: _float("MAX_DRAWDOWN_PCT", 15.0))
    min_reward_risk: float = field(default_factory=lambda: _float("MIN_REWARD_RISK", 1.5))
    min_model_samples: int = field(default_factory=lambda: _int("MIN_MODEL_SAMPLES", 20))
    min_expected_value_r: float = field(default_factory=lambda: _float("MIN_EXPECTED_VALUE_R", 0.05))
    loss_cooldown_min: int = field(default_factory=lambda: _int("LOSS_COOLDOWN_MIN", 60))
    max_consecutive_errors: int = field(default_factory=lambda: _int("MAX_CONSECUTIVE_ERRORS", 5))

    # --- strategy evaluation ----------------------------------------------------
    auto_disable_min_trades: int = field(default_factory=lambda: _int("AUTO_DISABLE_MIN_TRADES", 20))
    auto_disable_profit_factor: float = field(default_factory=lambda: _float("AUTO_DISABLE_PROFIT_FACTOR", 0.9))

    # --- live trading (disabled by default, see app/brokers/live.py) -------------
    live_trading_enabled: bool = field(default_factory=lambda: _bool("LIVE_TRADING_ENABLED", False))
    live_trading_confirmation: str | None = field(default_factory=lambda: _env("LIVE_TRADING_CONFIRMATION"))

    # --- process ----------------------------------------------------------------
    run_worker_in_api: bool = field(default_factory=lambda: _bool("RUN_WORKER_IN_API", False))

    @property
    def is_synthetic(self) -> bool:
        return self.data_mode == "synthetic"

    @property
    def account_name(self) -> str:
        return "TEST" if self.is_synthetic else "PAPER"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


def reset_settings_cache() -> None:
    get_settings.cache_clear()
