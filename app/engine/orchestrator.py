"""Pipeline: scan -> technical analysis -> signals -> risk -> execution -> persist.

Every step writes its timing/status into ``cycles.steps`` — that is the technical
proof that each step actually ran. When no trade happens the reason is stored.
"""
from __future__ import annotations

import logging
import time
import traceback

from app.agents.execution import ExecutionAgent
from app.agents.performance import PerformanceAnalyst
from app.agents.risk import RiskManager, kill_switch_state
from app.agents.scanner import MarketScanner
from app.agents.strategist import StrategyAnalyst
from app.agents.technical import TechnicalAnalyst
from app.config import get_settings
from app.data.hub import MarketData
from app.db import Cycle, StrategyState, now, session_scope
from app.engine.portfolio import ACCOUNT_LOCK, get_account, record_equity
from app.strategies import STRATEGIES

log = logging.getLogger(__name__)


def ensure_strategy_rows() -> None:
    with session_scope() as s:
        for name in STRATEGIES:
            if s.get(StrategyState, name) is None:
                s.add(StrategyState(name=name, enabled=True, stats={}))


class Orchestrator:
    def __init__(self, md: MarketData, account: str | None = None, is_test: bool | None = None,
                 agent_prefix: str = ""):
        st = get_settings()
        self.md = md
        self.account = account or st.account_name
        self.is_test = st.is_synthetic if is_test is None else is_test
        p = agent_prefix
        self.scanner = MarketScanner(md, prefix=p)
        self.technical = TechnicalAnalyst(md, prefix=p)
        self.strategist = StrategyAnalyst(md, prefix=p)
        self.risk = RiskManager(md, prefix=p)
        self.execution = ExecutionAgent(md, prefix=p)
        self.performance = PerformanceAnalyst(prefix=p)
        ensure_strategy_rows()
        with session_scope() as s:
            get_account(s, self.account)

    def run_cycle(self) -> dict:
        with session_scope() as s:
            cyc = Cycle(account=self.account, started_at=now(), status="running", steps={})
            s.add(cyc)
            s.flush()
            cid = cyc.id
        steps: dict[str, dict] = {}
        summary: dict = {"cycle_id": cid}

        def step(name: str, fn):
            t0 = time.time()
            try:
                out = fn()
                steps[name] = {"ok": True, "sec": round(time.time() - t0, 2), "at": now()}
                return out
            except Exception as e:
                steps[name] = {"ok": False, "sec": round(time.time() - t0, 2), "at": now(), "error": str(e)[:300]}
                raise

        try:
            ranked, scan_res = step("1_marktdaten_scan", lambda: self.scanner.run(cid))
            analyses = step("2_technische_analyse", lambda: self.technical.run(cid, ranked))
            signal_ids, strat_res = step("3_signale", lambda: self.strategist.run(cid, analyses, self.account, self.is_test))
            approved, risk_res = step("4_risikopruefung", lambda: self.risk.review(cid, signal_ids, self.account))
            executed, exec_res = step("5_ausfuehrung", lambda: self.execution.execute(cid, approved, self.account))
            mon = step("6_positionen", lambda: self.execution.monitor(self.account))

            def _save():
                with ACCOUNT_LOCK, session_scope() as s:
                    record_equity(s, get_account(s, self.account), force=True)
            step("7_speichern", _save)
            reason = None
            if not executed:
                with session_scope() as s:
                    ks = kill_switch_state(s)
                if ks.get("active"):
                    reason = f"Kill Switch aktiv: {ks.get('reason')}"
                elif not signal_ids:
                    reason = ("Keine Strategie-Bedingungen vollständig erfüllt. Nächste Setups: "
                              + "; ".join(strat_res.get("closest_setups", [])[:3]))
                elif not approved:
                    reason = "Alle Signale vom Risk Manager abgelehnt: " + " | ".join(risk_res.get("vetoes", [])[:3])
                else:
                    reason = "Freigegebene Orders nicht ausgeführt: " + "; ".join(exec_res.get("failed", [])[:3])
            summary.update({
                "markets_scanned": scan_res.get("markets_scanned", 0), "analysed": len(analyses),
                "signals": len(signal_ids), "approved": len(approved), "executed": len(executed),
                "closed": len(mon.get("closed", [])), "no_trade_reason": reason,
            })
            with session_scope() as s:
                c = s.get(Cycle, cid)
                c.finished_at = now()
                c.status = "ok"
                c.markets_scanned = summary["markets_scanned"]
                c.candidates = min(len(ranked), get_settings().deep_scan_top_n)
                c.analysed = len(analyses)
                c.signals = len(signal_ids)
                c.approved = len(approved)
                c.executed = len(executed)
                c.no_trade_reason = reason
                c.steps = steps
            self.risk.register_cycle_result(True, self.account)
            summary["status"] = "ok"
        except Exception as e:  # noqa: BLE001
            log.error("cycle %s failed: %s\n%s", cid, e, traceback.format_exc(limit=8))
            with session_scope() as s:
                c = s.get(Cycle, cid)
                c.finished_at = now()
                c.status = "error"
                c.error = f"{type(e).__name__}: {e}"[:2000]
                c.steps = steps
                c.no_trade_reason = f"Zyklus abgebrochen: {e}"[:500]
            self.risk.register_cycle_result(False, self.account)
            summary.update({"status": "error", "error": str(e)})
        return summary

    def run_monitor(self) -> dict:
        self.risk.check_account_limits(self.account)
        return self.execution.monitor(self.account)

    def run_performance(self) -> dict:
        return self.performance.run(self.account)
