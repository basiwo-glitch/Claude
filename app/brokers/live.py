"""Live (real money) trading — PREPARED BUT DISABLED.

No code path in this application sends orders to a real broker. The execution
agent always uses the PaperBroker. This module defines the gate that must be
passed before a future live broker adapter may even be constructed:

1. LIVE_TRADING_ENABLED=true in the environment (set by the owner on the server)
2. LIVE_TRADING_CONFIRMATION equals the exact phrase below
3. all automatic prerequisites (forward test length, trade count, positive
   expectancy, kill-switch test, risk limits) are fulfilled
4. owner confirmations stored in the DB (broker connection reviewed, regulatory
   requirements checked) — set via the API only with the dashboard password

Even if every gate is green, ``LiveBroker`` raises: a concrete broker adapter
(e.g. Binance spot with trade-only API keys, IP whitelisting, no withdrawal rights)
still has to be written, reviewed and tested by the owner.
"""
from __future__ import annotations

from sqlalchemy.orm import Session

from app.brokers.base import Broker
from app.config import get_settings
from app.db import EquitySnapshot, Position, get_state, now

CONFIRMATION_PHRASE = "ICH BESTAETIGE ECHTGELD-HANDEL AUF EIGENES RISIKO"
MIN_FORWARD_DAYS = 60
MIN_TRADES = 200


def live_gate_status(s: Session) -> dict:
    st = get_settings()
    checks = []

    first = s.query(EquitySnapshot.ts).filter(EquitySnapshot.account == "PAPER").order_by(EquitySnapshot.ts).first()
    days = (now() - first[0]) / 86400 if first else 0.0
    checks.append({"key": "forward_test_days", "label": f"Forward-Test ≥ {MIN_FORWARD_DAYS} Tage",
                   "ok": days >= MIN_FORWARD_DAYS, "value": f"{days:.1f} Tage"})

    rows = s.query(Position.pnl_eur, Position.r_multiple).filter(
        Position.account == "PAPER", Position.status == "closed", Position.is_test.is_(False)).all()
    n = len(rows)
    checks.append({"key": "trade_count", "label": f"≥ {MIN_TRADES} abgeschlossene Paper-Trades",
                   "ok": n >= MIN_TRADES, "value": str(n)})
    rs = [r for _, r in rows if r is not None]
    exp = sum(rs) / len(rs) if rs else None
    checks.append({"key": "expectancy", "label": "Positive Erwartung nach Kosten (Ø R > 0)",
                   "ok": exp is not None and exp > 0 and n >= MIN_TRADES,
                   "value": f"{exp:+.3f} R" if exp is not None else "—"})

    ks = get_state(s, "killswitch_test")
    checks.append({"key": "kill_switch_tested", "label": "Notabschaltung erfolgreich getestet (≤ 30 Tage)",
                   "ok": bool(ks.get("ok")) and now() - ks.get("at", 0) < 30 * 86400,
                   "value": ks.get("detail", "nie getestet")})

    risk_ok = (st.risk_per_trade_pct <= 1.0 and st.max_daily_loss_pct <= 5 and st.max_drawdown_pct <= 20
               and st.max_gross_exposure_pct <= 100)
    checks.append({"key": "risk_limits", "label": "Risikolimits konservativ (≤1 %/Trade, ≤5 %/Tag, ≤20 % DD, keine Hebel)",
                   "ok": risk_ok, "value": f"{st.risk_per_trade_pct} % / {st.max_daily_loss_pct} % / "
                                            f"{st.max_drawdown_pct} % / {st.max_gross_exposure_pct} %"})

    owner = get_state(s, "live_owner_confirmations")
    checks.append({"key": "broker_reviewed", "label": "Broker-Anbindung geprüft (Adapter implementiert & getestet)",
                   "ok": bool(owner.get("broker_reviewed")) and False,
                   "value": "kein Live-Broker-Adapter implementiert"})
    checks.append({"key": "regulatory", "label": "Regulatorische / steuerliche Anforderungen geprüft",
                   "ok": bool(owner.get("regulatory_checked")), "value": owner.get("regulatory_note", "offen")})
    checks.append({"key": "env_flag", "label": "LIVE_TRADING_ENABLED=true auf dem Server", "ok": st.live_trading_enabled,
                   "value": str(st.live_trading_enabled)})
    checks.append({"key": "env_confirmation", "label": "Bestätigungssatz in LIVE_TRADING_CONFIRMATION",
                   "ok": st.live_trading_confirmation == CONFIRMATION_PHRASE, "value": "gesetzt" if
                   st.live_trading_confirmation else "nicht gesetzt"})
    allowed = all(c["ok"] for c in checks)
    return {"mode": "paper", "live_allowed": allowed, "live_active": False, "checks": checks,
            "note": "Echtgeld-Modus ist deaktiviert. Es existiert kein Codepfad, der echte Orders sendet."}


class LiveTradingDisabled(RuntimeError):
    pass


class LiveBroker(Broker):  # pragma: no cover - intentionally unusable
    name = "live"
    is_live = True

    def __init__(self, s: Session):
        status = live_gate_status(s)
        if not status["live_allowed"]:
            raise LiveTradingDisabled("Live-Trading gesperrt: " + ", ".join(c["label"] for c in status["checks"]
                                                                            if not c["ok"]))
        raise LiveTradingDisabled("Kein Live-Broker-Adapter implementiert – bewusst nicht vorhanden.")
