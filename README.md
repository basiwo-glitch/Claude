# AI Trader – autonomes Multi-Agenten-Paper-Trading

Ein Hintergrund-Worker scannt rund um die Uhr Krypto- und Aktienmärkte, analysiert sie mit sechs spezialisierten Agenten und führt automatisch **Paper-Trades** aus (virtuelle 10.000 €, echte Kurse, Gebühren, Slippage). Das Dashboard (Dark Mode, iPad-tauglich) zeigt nur an, was wirklich passiert ist. Wenn eine Komponente nicht arbeitet, wird sie nicht grün angezeigt.

> **Kein Echtgeld.** Es gibt keinen Codepfad, der echte Orders sendet (siehe `app/brokers/live.py`).
> **Keine Gewinnversprechen.** Ob eine Strategie etwas taugt, zeigt allein der Forward-Test. Verlierende Strategien werden automatisch abgeschaltet.

## Architektur

```
            ┌──────────── Worker (python -m app.worker, 24/7, kein Browser nötig) ───────────┐
            │  alle 5 min: Zyklus           alle 30 s: Monitor        alle 15 min: Auswertung │
 Binance ──▶│  1 Market Scanner ─▶ 2 Technical Analyst ─▶ 3 Strategy Analyst ─▶ 4 Risk Manager │
 Yahoo   ──▶│        (≤1000 Märkte)     (1m…1d)              (Signale + gemessene     (Veto)  │
            │                                                 Wahrscheinlichkeit)       │     │
            │  6 Performance Analyst ◀── PostgreSQL ◀── 5 Execution Agent (Paper) ◀─────┘     │
            └────────────────────────────────────────────────────────────────────────────────┘
                                         ▲
            API + Dashboard (FastAPI) ───┘   ◀── HTTPS (Caddy) ◀── iPad / Browser
```

* **Worker** (`app/worker.py`): Scheduler mit DB-Lease (genau ein aktiver Worker), Heartbeat und Watchdog. Bei Hängern beendet sich der Prozess, Docker startet ihn neu.
* **PostgreSQL**: Trades, Orders, Signale, Zyklen (mit Nachweis jedes Schritts), Agentenentscheidungen, Equity-Kurve.
* **API/Dashboard** (`app/api`, `app/static`): FastAPI mit HTTP-Basic-Auth. Das Dashboard ist eine eigene Canvas-Chart-Engine ohne Build-Schritt.
* Redis und Next.js habe ich bewusst weggelassen: Die Datenbank übernimmt Locks und Idempotenz, und das Frontend braucht keinen eigenen Build- und Deploy-Schritt. Das sind weniger bewegliche Teile.

## Die sechs Agenten

| # | Agent | Was er tatsächlich tut |
|---|---|---|
| 1 | Market Scanner | 1 Request für alle Binance-USDT-Paare und Tageskerzen für das Aktien-Universum. Filtert Stablecoins, Hebel-Token und illiquide Märkte. Robuster Z-Score der Kursänderung, dann für die Top 40 1h-Kerzen: Volumenanstieg (letzte Stunde vs. 24-h-Schnitt), ungewöhnliche Bewegung (Return / 48-h-Volatilität), Trend. Ergebnis ist eine Rangliste. |
| 2 | Technical Analyst | Top 12 auf 1m/5m/15m/1h/4h/1d: RSI, MACD, EMA 20/50/200, Bollinger, ATR, ADX, Volumenfaktor, Candlestick-Muster, Unterstützungs- und Widerstandszonen (Swing-Cluster), Marktstruktur (HH/HL), Volatilität, Multi-Timeframe-Ausrichtung. |
| 3 | Strategy Analyst | Prüft 6 Strategien × Long/Short auf der letzten **geschlossenen** Kerze. **Wahrscheinlichkeiten werden gemessen, nicht erfunden:** Dieselbe Regel wird auf der Historie aller analysierten Märkte nachgespielt (ohne Überlappung, Stop zuerst bei Doppeltreffer, Gaps zum Open, Kosten abgezogen). Daraus ergeben sich Trefferquote mit 95 %-Konfidenzintervall, Stichprobengröße und Erwartungswert in R nach Kosten. Ab 10 Forward-Trades gilt der vorsichtigere Wert. Erstellt außerdem Long- und Short-Szenarien je Markt. |
| 4 | Risk Manager | Vetorecht. Prüft: Kill Switch, Tagesverlust (3 %), Drawdown (15 % → Notstopp), Duplikate, max. 8 Positionen, Verlust-Abkühlphase, Markt offen, Kurs frisch (≤ 180 s), Kurs nahe Signal (≤ 0,5 ATR), n ≥ 20, EV ≥ 0,05 R, CRV. Positionsgröße: 0,5 % Risiko pro Trade, max. 15 % pro Position, max. 80 % Exposure, keine Hebel. Fünf fehlgeschlagene Zyklen in Folge aktivieren den Kill Switch. |
| 5 | Execution Agent | Paper-Fills auf den aktuellen Kurs mit Slippage zu Ihren Ungunsten und Gebühren. Idempotente Order-ID (Strategie + Markt + Seite + Kerze) mit Unique-Constraint verhindert Doppelorders. Überwacht Positionen anhand jeder 1m-Kerze: Stop (bei Gap zum Open), Take-Profit (Limit), Zeitstopp, Glattstellung per Kill Switch. |
| 6 | Performance Analyst | Trefferquote (mit KI), Profit Factor, Erwartung, Drawdown, Kosten, Vergleich der Strategien, Modell-Kalibrierung, Fehlermuster (schnelle Stop-Outs, Kostenanteil, Zeitfenster, Long/Short-Asymmetrie). Jede Erkenntnis kommt mit Beleg und Stichprobengröße. Deaktiviert Strategien automatisch bei ≥ 20 Trades, PF < 0,9 und negativer Erwartung. |

„KI“ heißt hier: spezialisierte statistische und regelbasierte Agenten mit überprüfbaren Modellen. Ein externes Sprachmodell entscheidet nichts. Das ließe sich als zusätzliche Kommentarebene ergänzen, wäre aber nicht nachprüfbar.

## Strategien

Scalping (5m), Daytrading (15m), Momentum (1h), Trend Following (4h / Aktien 1d), Breakout (1h), Mean Reversion (15m). Alle Regeln stehen in `app/strategies/library.py`, jede Bedingung erscheint im Dashboard mit ✓/✗ und Wert. Jede Strategie wird separat ausgewertet und kann im Dashboard an- und ausgeschaltet werden.

## Installation (empfohlen: eigener Server, ca. 4–6 €/Monat)

1. Einen Ubuntu-Server anlegen (z. B. Hetzner CX22).
2. Dieses Verzeichnis hochladen (`scp -r ai-trader root@SERVER:/opt/`) oder aus einem Git-Repo klonen.
3. `sudo bash /opt/ai-trader/deploy/setup_vps.sh`

Das Skript installiert Docker, erzeugt Passwörter, startet PostgreSQL, Worker, API und Caddy (HTTPS über Let's Encrypt; ohne eigene Domain über `<ip>.sslip.io`). Danach prüft es echte Marktdaten (`app.datacheck`), führt alle Tests aus und den kontrollierten Selbsttest. Am Ende stehen URL und Passwort in der Ausgabe.

Hinweis: `api.binance.com` sperrt US-IP-Adressen (HTTP 451). Das System weicht dann automatisch auf `data-api.binance.vision` aus. Ein Server in Deutschland ist trotzdem die beste Wahl.

**Lokal ausprobieren:** `cp .env.example .env` (Werte setzen), dann `docker compose up -d --build`.
**Ohne echte Daten (Demo):** `MARKET_DATA_MODE=synthetic`. Alles läuft dann auf dem Konto TEST, und das Dashboard zeigt einen gelben Warnbalken.

## Nachweise / Tests

`python -m pytest -q tests`: Indikatoren gegen pandas-Referenz, Replay-Logik, Risikoprüfungen, Fills und G&V (long/short, Gap, Stop-zuerst, Zeitstopp, Glattstellung), Doppelorder-Schutz, Tagesverlust- und Drawdown-Stopp, Datenanbieter mit simulierten API-Antworten (Fail-over, 429, Circuit Breaker), API-Auth, Worker im Hintergrund mit Lease, vollständige Pipeline über 16 simulierte Stunden.

`python -m app.selftest`: kontrollierter Integrationstest mit künstlichem Datensatz. Eine Order **muss** ausgelöst und per Take-Profit geschlossen werden. Die G&V wird aus den Orders nachgerechnet. Alles läuft auf dem Konto `SELFTEST` mit `is_test=true` und zählt nie zur Performance. Der Test lässt sich auch im Dashboard unter System starten.

`python -m app.datacheck`: prüft, dass echte Kurse von Binance und Yahoo ankommen und frisch sind.

## Bekannte Grenzen

* Yahoo Finance ist eine inoffizielle, rate-limitierte Quelle. Bei Ausfällen fallen nur die Aktien weg, Krypto läuft weiter. Ein Anbieter mit API-Schlüssel lässt sich über `MarketDataProvider` anbinden.
* Paper-Fills sind ein Modell (Kurs + Slippage). Echte Orderbücher, Teilausführungen und Leihgebühren für Shorts werden nicht simuliert.
* Der Kill Switch nach Fehlerserien bleibt aktiv, bis Sie ihn bewusst aufheben.
