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

### Vollständig implementiert und getestet (133 Unit-Tests, grün)

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

**Nichts davon lief in dieser Sandbox gegen einen echten Endpoint** — kein
Netzwerkzugriff im Build-Container. Jede Netzwerk-Funktion ist an der reinen
Request/Response-Grenze mit Fixtures getestet; die Jupiter-API-Struktur wurde
gegen aktuelle Dokumentation (Stand 2026-07-21) verifiziert, nicht nur aus dem
Trainingsstand übernommen. Vor Live-Einsatz: gegen Devnet oder mit einem
Trivialbetrag auf Mainnet verifizieren. `db/repository.py`s Queries zusätzlich:
gegen echtes Postgres testen (`docker compose up -d postgres` + `schema.sql`),
das gehört noch vor jeden produktiven Einsatz — auch Paper-Trading.

### Gerüst mit definierten Schnittstellen (Anbindung fehlt)

`ingest/` EVM- und Social-Adapter, `execution/` Uniswap/PancakeSwap-Adapter,
`api/` Endpunkt-Bodies (liest/schreibt noch nicht gegen `db/repository.py`),
`enrich/` Holder-Analyse + DexScreener/Birdeye-Clients, `scoring/worker.py`
(der Bus-Worker, der `Scorer.score()` aufruft und auf das in
`risk/gate_service.py` definierte `SignalMessage`-Format publiziert).

Diese Services laufen in `docker-compose.yml` bewusst nicht im Default-Profil
(`profiles: ["full"]`) — sie würden sonst crash-loopen statt einfach nichts zu
tun, weil `ingest/runner.py` unbekannte Quellen laut ablehnt statt still zu
schweigen. Das ist die richtige Fehlerbehandlung für eine unfertige
Komponente, aber die falsche Default-Erfahrung für „einmal antippen und es
läuft" — daher die Trennung. `docker compose --profile full up` schaltet sie
explizit dazu.

Ohne `enrich/` + `scoring/worker.py` fließen keine neuen Kandidaten durch die
Pipeline — alles ab `risk/gate_service.py` abwärts ist fertig verdrahtet und
wartet auf Nachrichten, die aktuell niemand publiziert. `portfolio/manager.py`s
Exit-Loop arbeitet trotzdem schon live: sobald irgendeine Position existiert
(z. B. manuell für einen Test angelegt), wertet er sie jeden Tick echt aus,
inklusive einer echten Sell-Simulation als Struktur-Signal — nur der
Liquiditäts-Drain-Check bleibt bis zur `enrich`-Anbindung deaktiviert (siehe
Code-Kommentar in `portfolio/manager.py`, absichtlich kein Platzhalterwert).

### In dieser Runde gefundene und behobene Inkonsistenzen

- **`price_usd` war USD pro Base-Unit statt USD pro ganzem Token** — in
  `_build_fill()` UND `quote()` von `execution/solana_executor.py`. Bei einem
  9-Dezimalstellen-Token ein Faktor-10⁹-Fehler, der jede PnL- und
  Stop-Loss-Berechnung stumm korrumpiert hätte. Aufgefallen beim Verdrahten der
  DB-Schicht, gefixt mit einem dezimalstellen-bewussten Cache
  (`_get_decimals()`, on-chain via `getTokenSupply`), zwei Regressionstests.
- **`JupiterClient.mid_price()` hatte den gleichen Fehlertyp** — SOL (9
  Dezimalstellen) gegen USDC (6) ohne Anpassung verglichen, macht den
  Rückgabewert um Faktor 1000 falsch. Nie benutzt, nie getestet — ersetzt durch
  `sol_usd_price()`, das die Dezimalstellen korrekt behandelt und tatsächlich
  vom Position Manager verwendet wird.
- **`LAMPORTS_PER_SOL` war in zwei Dateien dupliziert** — in `jupiter.py`
  zentralisiert, `solana_executor.py` importiert es jetzt.
- **Ein Bug, den ich selbst eingebaut und noch im selben Durchgang gefangen
  habe**: ein früher Entwurf von `portfolio/manager.py` leitete `liquidity_usd`
  aus `price_usd` ab — hätte den Liquiditäts-Drain-Exit lautlos wirkungslos
  gemacht (Verhältnis immer ≈1, egal was mit der echten Poolliquidität passiert).
  Ersetzt durch einen ehrlichen No-Signal-Default plus einer echten
  Sell-Simulations-Probe für das Strukturprüfungs-Signal, das diese Komponente
  tatsächlich verlässlich liefern kann.
- `.env.example` und `config/prometheus.yml` waren seit Runde 1 in README bzw.
  `docker-compose.yml` referenziert, aber nie angelegt — beide fehlten, beide
  jetzt da.

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
