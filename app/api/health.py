"""Component health. A component is only 'green' when there is fresh proof that it works."""
from __future__ import annotations

from app.agents.base import AGENTS
from app.config import get_settings
from app.db import AgentState, db_ping, get_state, now, session_scope


def _c(name: str, status: str, detail: str, **extra) -> dict:
    return {"name": name, "status": status, "detail": detail, **extra}


def _age(ts: float | None) -> float | None:
    return None if not ts else now() - ts


def _fmt_age(sec: float | None) -> str:
    if sec is None:
        return "nie"
    if sec < 90:
        return f"vor {sec:.0f} s"
    if sec < 5400:
        return f"vor {sec / 60:.0f} min"
    return f"vor {sec / 3600:.1f} h"


def system_health() -> dict:
    st = get_settings()
    comps: list[dict] = []
    try:
        db_ping()
        comps.append(_c("Datenbank", "green", "Abfrage erfolgreich"))
    except Exception as e:  # noqa: BLE001
        comps.append(_c("Datenbank", "red", f"nicht erreichbar: {e}"))
        return {"overall": "red", "components": comps, "at": now()}

    with session_scope() as s:
        hb = get_state(s, "worker_heartbeat")
        ks = get_state(s, "kill_switch", {"active": False})
        halt = get_state(s, f"halt:{st.account_name}", {"active": False})
        agents = {a.name: a for a in s.query(AgentState).all()}
        selftest = get_state(s, "selftest")

    hb_age = _age(hb.get("ts"))
    if hb_age is not None and hb_age < 30 and hb.get("leader"):
        comps.append(_c("Hintergrund-Worker", "green", f"Heartbeat {_fmt_age(hb_age)} (PID {hb.get('pid')})"))
    elif hb_age is not None and hb_age < 30:
        comps.append(_c("Hintergrund-Worker", "yellow", "läuft, wartet auf Lease (zweite Instanz?)"))
    else:
        comps.append(_c("Hintergrund-Worker", "red", f"kein Heartbeat ({_fmt_age(hb_age)})"))

    jobs = hb.get("jobs", {}) if hb_age is not None and hb_age < 120 else {}
    limits = {"cycle": st.cycle_interval_sec * 2 + st.cycle_timeout_sec,
              "monitor": st.monitor_interval_sec * 4 + 60,
              "performance": st.performance_interval_sec * 2 + 300}
    labels = {"cycle": "Analysezyklus (Scan→Analyse→Signal→Risiko→Order)", "monitor": "Positionsüberwachung",
              "performance": "Performance-Auswertung"}
    for key, label in labels.items():
        j = jobs.get(key)
        if not j:
            comps.append(_c(label, "red", "keine Daten vom Worker"))
            continue
        age = _age(j.get("last_end"))
        if j.get("status") == "running" and (j.get("overdue_sec") or 0) <= 0:
            prev_ok = j.get("runs", 0) > 0 and (j.get("failures", 0) < j.get("runs", 0))
            comps.append(_c(label, "green" if prev_ok else "yellow", "läuft gerade", job=j))
        elif j.get("status") == "ok" and age is not None and age < limits[key]:
            comps.append(_c(label, "green", f"zuletzt erfolgreich {_fmt_age(age)} ({j.get('duration')} s)", job=j))
        elif j.get("status") == "error":
            comps.append(_c(label, "red", f"Fehler: {j.get('error')}", job=j))
        else:
            comps.append(_c(label, "yellow" if j.get("runs") else "red",
                            f"letzter Lauf {_fmt_age(age)} (Status {j.get('status')})", job=j))

    md = hb.get("market_data", {}) if hb_age is not None and hb_age < 120 else {}
    feeds = md.get("feeds", {})
    expected = ["synthetic"] if st.is_synthetic else (["binance"] if st.enable_crypto else []) + (
        ["yahoo"] if st.enable_stocks else [])
    for f in expected:
        info = feeds.get(f)
        label = {"binance": "Marktdaten Krypto (Binance)", "yahoo": "Marktdaten Aktien (Yahoo)",
                 "synthetic": "Marktdaten SYNTHETISCH (Testmodus)"}[f]
        if not info:
            comps.append(_c(label, "red", "noch keine erfolgreiche Abfrage"))
            continue
        ok_age = _age(info.get("last_ok"))
        err_age = _age(info.get("last_error_at"))
        window = st.cycle_interval_sec * 2 + 120
        if ok_age is not None and ok_age < window:
            status = "green" if (err_age is None or err_age > ok_age or err_age > 600) else "yellow"
            detail = f"letzte Daten {_fmt_age(ok_age)}, {info.get('last_tickers') or '–'} Märkte, Latenz {info.get('latency_ms')} ms"
            if status == "yellow":
                detail += f" · Fehler {_fmt_age(err_age)}: {info.get('last_error')}"
            if f == "synthetic":
                status = "yellow"
                detail = "SYNTHETISCHE DATEN – keine echten Kurse. " + detail
            comps.append(_c(label, status, detail))
        else:
            comps.append(_c(label, "red", f"keine frischen Daten ({_fmt_age(ok_age)}); letzter Fehler: {info.get('last_error')}"))

    for name, meta in AGENTS.items():
        a = agents.get(name)
        if a is None:
            comps.append(_c(f"Agent {meta['no']} {meta['title']}", "red", "noch nie gelaufen"))
            continue
        succ = _age(a.last_success)
        window = (st.performance_interval_sec if name == "performance" else
                  st.monitor_interval_sec * 4 if name == "execution" else st.cycle_interval_sec) * 2 + 300
        if a.status == "error":
            stt = "red"
        elif a.status in ("warning", "halted"):
            stt = "yellow"
        elif succ is not None and succ < window:
            stt = "green"
        elif a.status == "working":
            stt = "green" if succ is not None and succ < window * 2 else "yellow"
        else:
            stt = "yellow" if succ is not None else "red"
        comps.append(_c(f"Agent {meta['no']} {meta['title']}", stt,
                        f"Status {a.status}, letzter Erfolg {_fmt_age(succ)}" + (f" · Fehler: {a.last_error}" if a.status == "error" else "")))

    comps.append(_c("Kill Switch", "red" if ks.get("active") else "green",
                    f"AKTIV: {ks.get('reason')}" if ks.get("active") else "nicht aktiv"))
    comps.append(_c("Risikolimits", "red" if halt.get("active") else "green",
                    halt.get("reason") if halt.get("active") else "innerhalb der Limits"))
    if selftest:
        comps.append(_c("Integrations-Selbsttest", "green" if selftest.get("ok") else "red",
                        f"{'bestanden' if selftest.get('ok') else 'FEHLGESCHLAGEN'} {_fmt_age(_age(selftest.get('at')))}"))
    comps.append(_c("Echtgeld-Modus", "green", "deaktiviert (nur Paper-Trading)"))

    worst = "green"
    for c in comps:
        if c["status"] == "red":
            worst = "red"
            break
        if c["status"] == "yellow":
            worst = "yellow"
    return {"overall": worst, "components": comps, "at": now(), "mode": st.data_mode, "account": st.account_name,
            "worker": {k: hb.get(k) for k in ("ts", "pid", "host", "started_at", "leader", "mode")},
            "market_data": md}
