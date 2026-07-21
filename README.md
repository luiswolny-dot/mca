# Meme-Coin Alpha (MCA)

Event-getriebenes, mehrkettiges Trading-System für Meme-Coins auf Solana, Base,
BNB Chain und Ethereum.

> **Neu hier oder den Überblick verloren?** [`docs/UEBERBLICK.md`](docs/UEBERBLICK.md)
> zeigt in einem Bild + einer Tabelle, was jeder Ordner tut und was heute
> wirklich passiert, wenn der Bot startet — bevor du dich in einzelne Dateien vertiefst.

> **Bitte zuerst [`docs/RISKS.md`](docs/RISKS.md) lesen.** Dort steht die
> Kostenrechnung, die darüber entscheidet, ob so ein System überhaupt profitabel
> sein *kann*. Dies ist technische Dokumentation, keine Anlageberatung.

---

## Was hier tatsächlich lauffähig ist

Ehrlichkeit vor Vollständigkeit: ein produktionsreifes System dieser Art ist
Monate Arbeit für ein kleines Team, vor allem wegen der Chain-Integrationen und
der Datenbeschaffung. Was in diesem Repository steht, teilt sich so auf:

### Vollständig implementiert und getestet (158 Unit-Tests, grün)

| Modul | Inhalt |
|---|---|
| `domain/models.py` | Alle Kerntypen, punktgenaue Zeitsemantik, `PoolCreatedEvent` |
| `scoring/gates.py` | 20+ Hard Gates, fail-closed bei `None` |
| `scoring/scorer.py` | 7 Feature-Gruppen → Score 0–100 → EV-Entscheidung |
| `features/technicals.py` | RSI, EMA, MACD, VWAP, ATR, OFI, gedämpftes Momentum |
| `risk/sizing.py` | Exakte x·y=k-Impactmathematik, fraktionales Kelly, ATR-Sizing |
| `risk/limits.py` | Circuit Breaker, alle Portfolio-Limits |
| `portfolio/exits.py` | Exit-Zustandsautomat inkl. struktureller On-Chain-Exits |
| `backtest/metrics.py` | Sharpe, Sortino, Calmar, **Deflated Sharpe**, Ulcer |
| `backtest/fill_model.py` | Latenz, Impact, TX-Fehlschläge, Survivorship-Audit |
| `ml/calibration.py` | Purged CV mit Embargo, Isotonic-Kalibrierung |
| `db/schema.sql` | Vollständiges Schema, TimescaleDB, append-only Audit |
| `core/config.py` | YAML+Env-Loader, Pydantic-Validierung, Konverter in **jede** obige Dataclass |
| `core/retry.py` | Backoff mit Jitter, retryable vs. fatal — abhängigkeitsfrei |
| `core/bus.py` | Redis-Streams-Wrapper: die Queue zwischen allen Pipeline-Stufen |
| `execution/wallet.py` | scrypt+AES-256-GCM Keystore — Kryptografie Ende-zu-Ende getestet |
| `execution/solana_rpc.py` | JSON-RPC-Client + `logsSubscribe`-WebSocket mit Reconnect |
| `execution/jupiter.py` | Quote/Swap-Client gegen die aktuelle `/swap/v1`-API |
| `execution/solana_executor.py` | Konkreter `Executor`: Round-Trip-Honeypot-Check, Shadow-Gate live verdrahtet |
| `execution/worker.py` | Bus-Worker: Intents → `Executor.execute()` → Fills, eskaliert fehlgeschlagene Sells |
| `ingest/solana_pools.py` | Neue-Pool-Erkennung: Log-Filter + Transaktions-Bestätigung, fail-closed |
| `alerts/telegram.py` | Telegram-Channel für den bestehenden `Notifier` |
| `db/repository.py` | SQLAlchemy-Core-Zugriffsschicht — Mapper vollständig getestet, Queries nicht (kein DB im Sandbox) |
| `risk/gate_service.py` | Bus-Worker: Signals → `RiskGate.check_entry()` → autorisierte Intents, einzige Instanz |
| `portfolio/manager.py` | Fill→Position-Reconciliation + Exit-Loop, ruft `ExitEngine.evaluate()` live |
| `ops/watchdog.py` | Heartbeat-Überwachung, RPC-Health, HALT-Trigger, Risk-Event-Alerting |
| `ops/tasks.py` | Celery: `roll_day`, Wallet-Sweep-Erkennung, Modell-Retraining |
| `start.sh` | Ein-Kommando-Start: Secrets-Check, Config, Preflight-Tests, `docker compose up` |
| `enrich/onchain.py` | Mint-/Freeze-Authority + Holder-Konzentration direkt per RPC, kein Drittanbieter |
| `enrich/dexscreener.py` | Liquidität/Volumen/Preis — API-Shape gegen aktuelle Doku verifiziert (Stand 2026-07-21) |
| `enrich/worker.py` | Pool-Event → vollständiger `Candidate`, fail-closed bei fehlenden Daten |
| `scoring/worker.py` | `Candidate` → Score → EV-Entscheidung → `Signal`, mit selbst-aufwärmendem Cohort |

**Nichts davon lief in dieser Sandbox gegen einen echten Endpoint** — kein
Netzwerkzugriff im Build-Container. Jede Netzwerk-Funktion ist an der reinen
Request/Response-Grenze mit Fixtures getestet; DexScreener- und Jupiter-API
wurden gegen aktuelle Dokumentation verifiziert, nicht aus dem Trainingsstand
übernommen. Vor Live-Einsatz: gegen Devnet/Mainnet-Trivialbetrag verifizieren.
`db/repository.py`s Queries zusätzlich gegen echtes Postgres testen.

### Gerüst mit definierten Schnittstellen (Anbindung fehlt)

`ingest/` EVM- und Social-Adapter, `execution/` Uniswap/PancakeSwap-Adapter,
`api/` Endpunkt-Bodies (liest/schreibt noch nicht gegen `db/repository.py`),
Whale-/Smart-Money-Flow (`FlowSnapshot` bleibt `None`, siehe `enrich/worker.py`s
Kommentar), Social-Signale (`SocialSnapshot` bleibt `None`, `ingest-social`
existiert nicht).

`docker compose --profile full up` schaltet die EVM-/Social-Ingest-Prozesse
zusätzlich dazu (sie lehnen unbekannte Quellen laut ab statt zu crash-loopen —
richtig, aber nicht die Default-Erfahrung).

**Der Live-Pfad für Solana ist jetzt Ende-zu-Ende verdrahtet:** Ingest → Enrich
→ Score → Risk Gate → Executor → Position Manager, jede Stufe über den Bus
verbunden, jede Stufe einzeln getestet. Was fehlt, ist kein fehlendes Stück
mehr, sondern fehlende Tiefe an zwei Stellen: `score.win_probability` bleibt
`None` bis ein Modell trainiert ist (kein Trade ohne kalibrierte
Erfolgswahrscheinlichkeit, siehe `should_trade()`), und Social/Flow-Signale
fehlen als Score-Komponenten — beides degradiert ehrlich (neutral bzw. „nicht
handelbar"), nichts wird geraten.

### In dieser Runde gefundene und behobene Inkonsistenzen

- **Top-Holder-Konzentration hätte ohne Ausschluss des Pool-Vaults auf jedem
  einzigen frischen Launch ~90 % angezeigt** — der Pool selbst hält direkt
  nach der Erstellung den Großteil der Supply, das ist keine Wal-Bewegung.
  `PoolCreatedEvent` bekam ein `base_vault`-Feld (aus dem bereits vorhandenen,
  aber ungenutzten `coin_vault`-Index), `enrich/onchain.py` schließt es explizit
  aus. Test dokumentiert beide Fälle (mit/ohne Ausschluss).
- **DexScreeners `liquidity.base`/`quote` sind dezimalstellen-angepasste
  Werte, `MarketSnapshot.pool_reserve_base` erwartet rohe Base-Units** —
  derselbe Fehlertyp wie die `price_usd`-Bugs aus der vorigen Runde, diesmal
  vor dem Schreiben gefunden statt danach, weil gezielt danach gesucht wurde.
  Regressionstest mit konkreten Zahlen.
- Mint-Authority-Prüfung nutzt `getAccountInfo` mit `jsonParsed`-Encoding statt
  eines selbstgeschriebenen Byte-Offset-Parsers — der RPC-Node kennt sowohl
  das klassische SPL-Token- als auch das Token-2022-Layout korrekt, ein
  handgeschriebener fixed-offset-Parser hätte bei Token-2022 (zunehmend
  verbreitet) still falsche Werte geliefert statt sauber `None`.

---

## Die vier Entscheidungen, die das System prägen

**1. Hard Gates sind Vetos, keine Score-Beiträge.**
Ein Token mit explosivem Volumen und aktiver Mint Authority erreicht in einem
gewichteten Modell 85/100 und wird gekauft. Danach mintet der Deployer das
Zehnfache der Supply. Sicherheitseigenschaften sind nicht gegen Momentum
substituierbar, also sind sie Vorbedingungen. Ein `None` gilt als Fehlschlag,
nicht als „unbekannt, also okay".

**2. Der Score entscheidet nicht — der Erwartungswert entscheidet.**
```
EV = p·E[Gewinn] − (1−p)·E[Verlust] − Kosten
```
Bei 500 USD Position in einem 25 000-USD-Pool liegen die Round-Trip-Kosten bei
über 500 bps. Eine Strategie mit 55 % Trefferquote und symmetrischer Auszahlung
verliert damit Geld. Der Score ist ein Ranking-Instrument; gehandelt wird nur
bei positivem EV nach *allen* Kosten.

**3. Die Positionsgröße ist das Minimum aus vier unabhängigen Caps.**
Risiko-pro-Trade, Liquiditäts-Impact, fraktionales Kelly, absolutes Limit.
Welcher Cap gebunden hat, wird protokolliert: wenn in 90 % der Fälle der
Liquiditäts-Cap bindet, liegt die reale Kapazität der Strategie weit unter der
Kontogröße — das will man aus dem Dashboard erfahren, nicht aus einem Drawdown.

**4. Live-Handel ist eine Konstruktor-Invariante, kein Config-Flag.**
`LiveExecutorGuard` verweigert das Laden von Schlüsseln ohne 14 Tage
ununterbrochenes Paper Trading mit ≥100 Fills und unverändertem `strategy_hash`.
Jede Parameteränderung erzeugt einen neuen Hash und startet die Frist neu.

---

## Schnellstart

```bash
git clone <repo> && cd mca
./start.sh
```

`start.sh` prüft Docker, legt `.env` und `config/config.yaml` aus den
Beispieldateien an (und stoppt dort mit einer klaren Anleitung, falls
Secrets fehlen), führt die Test-Suite als Preflight-Check aus und startet
dann `docker compose up -d --build`. Das ist die ehrliche Version von „einmal
antippen" für ein selbst gehostetes System: ein Server-Kommando statt zehn.

Von Hand, äquivalent:
```bash
cp config/config.example.yaml config/config.yaml
cp .env.example .env            # Secrets ausfüllen
python scripts/run_tests.py     # -> 133/133 passed, ohne jede Abhängigkeit außer numpy
docker compose up -d --build    # startet im Paper-Modus; enrich/scorer/social
                                 # sind noch nicht gebaut, siehe unten — mit
                                 # `--profile full` explizit dazuschalten
open http://localhost:3001      # Grafana
```

`system.mode` steht per Default auf `paper`. Auf `live` umzustellen genügt
nicht — siehe Punkt 4 oben, durchgesetzt von `execution/base.py`, nicht nur
dokumentiert.

---

## Implementierungsreihenfolge

Die Reihenfolge ist nicht beliebig; jeder Schritt macht den nächsten überhaupt
erst überprüfbar.

**Phase 1 — Datenfundament (2–3 Wochen).**
Ingestor für *eine* Chain (Solana, per Geyser oder `logsSubscribe`) plus
DexScreener als zweite, unabhängige Quelle. Ziel: `market_snapshots` und
`swap_events` füllen sich lückenlos. Noch kein Scoring. Erfolgskriterium: 72
Stunden ohne Lücke im Zeitindex.

**Phase 2 — Enrichment und Gates (2 Wochen).**
Mint-/Freeze-Authority, LP-Lock, Holder-Verteilung, und vor allem die
**Sell-Simulation** in der beabsichtigten Ordergröße. Gates laufen im
Trockenmodus; das Rejection-Histogramm wird täglich gelesen. Erfolgskriterium:
manuelle Stichprobe von 30 abgelehnten Token bestätigt die Ablehnung.

**Phase 3 — Scoring und Paper Trading (2 Wochen).**
Scorer aktivieren, `PaperExecutor` gegen Live-Preise. Noch kein ML — feste
Gewinnwahrscheinlichkeit von 0.40 einsetzen, damit der EV-Pfad geübt wird.
Erfolgskriterium: 100+ Paper-Trades mit plausibler Verteilung.

**Phase 4 — Backtest und Kalibrierung (2–3 Wochen).**
Historische Swap-Events beschaffen (**inklusive toter Token** — sonst ist alles
wertlos), `SurvivorshipAudit` bestehen, Walk-Forward-Optimierung, Deflated
Sharpe mit ehrlicher `n_trials`. Danach ML-Modell trainieren, aber nur
ausrollen, wenn AUC ≥ 0.56 und Brier ≤ 0.24.

**Phase 5 — Live, klein (unbefristet).**
Ein Chain, eine Position gleichzeitig, Beträge, deren Totalverlust folgenlos ist.
Täglicher Abgleich zwischen `fills` und On-Chain-Balance. Erst nach 200+
Live-Trades über Skalierung nachdenken.

Der wichtigste und meistübersprungene Schritt ist der Vergleich der
Paper-Verteilung mit der Backtest-Verteilung. Weichen sie deutlich ab, ist das
Backtest-Setup falsch — nicht der Markt.

---

## Projektstruktur

```
mca/
├── src/mca/
│   ├── core/          Config, Logging, Bus, Clock
│   ├── domain/        models.py — gemeinsame Sprache aller Module
│   ├── db/            schema.sql, Repositories, Migrationen
│   ├── ingest/        je Quelle ein Adapter → TokenEvent
│   ├── enrich/        Security, Holder, Wallet-Labels
│   ├── features/      technicals.py, Point-in-Time Feature Store
│   ├── scoring/       gates.py, scorer.py
│   ├── risk/          sizing.py, limits.py
│   ├── execution/     base.py + Chain-Adapter
│   ├── portfolio/     exits.py, manager.py
│   ├── ml/            calibration.py
│   ├── backtest/      fill_model.py, metrics.py, engine.py
│   ├── api/           app.py
│   ├── alerts/        notifier.py
│   └── ops/           watchdog, Celery-Tasks
├── tests/unit/        45 Tests, alle grün
├── config/            config.example.yaml
├── docs/              ARCHITECTURE.md, RISKS.md
├── scripts/           run_tests.py (abhängigkeitsfrei)
├── dashboard.jsx      Operator-Terminal (React)
├── docker-compose.yml
└── Dockerfile         Tests laufen zur BUILD-Zeit
```

Abhängigkeitsregel: `domain` importiert nichts. `scoring`/`risk`/`portfolio`
importieren nur `domain` und `features`. Nur `execution` kennt Chain-SDKs.
Deshalb läuft der komplette Entscheidungskern ohne Netzwerk und ohne Datenbank
in unter zwei Sekunden durch die Testsuite.

---

## Sicherheit

- Der **Executor ist der einzige** Container mit gemountetem Keystore und hat
  keinen eingehenden Port. Die **API hat kein Schlüsselmaterial**. Ein RCE in der
  HTTP-Schicht erreicht das Wallet nicht.
- Hot Wallet ist ein Spending Limit (`hot_wallet_max_usd`), Überschuss wird auf
  eine Cold-Adresse gesweept.
- Keystore: scrypt (n=2^18), Datei-Mount als Docker Secret, `chmod 600`.
- `fills` und `risk_events` sind per Trigger append-only. Positionen sind
  abgeleiteter Zustand und jederzeit aus den Fills rekonstruierbar.
- Entsperren eines ausgelösten Circuit Breakers erfordert TOTP plus schriftliche
  Begründung und landet unveränderlich im Audit-Log. Die Reibung ist Absicht.

---

## Bewusst nicht implementiert

Sandwich-Angriffe, Frontrunning fremder Pending-Transaktionen, Wash Trading zur
Volumensimulation, koordinierte Social-Kampagnen für eigene Positionen. Das
Smart-Money-Tracking wertet ausschließlich **bereits bestätigte, öffentliche**
On-Chain-Transaktionen aus.

Prüffrage: *Beruht der Gewinn auf besserer Prognose oder darauf, dass jemand
anders schlechter ausgeführt wird, weil ich es so eingerichtet habe?* Nur
Ersteres ist hier gebaut.

---

## Lizenz und Haftung

Ohne Gewährleistung. Automatisierter Handel mit hochvolatilen Meme-Coins kann
zum vollständigen Verlust des eingesetzten Kapitals führen — bei diesem Assettyp
ist das nicht der unwahrscheinliche Fall. Steuerliche und regulatorische
Pflichten liegen beim Betreiber und unterscheiden sich je nach Land erheblich.
