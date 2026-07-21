# Risiken und ehrliche Erwartungshaltung

Dieses Dokument steht bewusst vor der Installationsanleitung. Wer es überspringt,
sollte das System nicht mit echtem Geld betreiben.

> Das hier ist eine technische Dokumentation, keine Anlageberatung. Ich bin kein
> Finanzberater. Automatisierter Handel mit hochvolatilen Meme-Coins kann zum
> Totalverlust des eingesetzten Kapitals führen, und das ist bei diesem Assettyp
> nicht das unwahrscheinliche Szenario.

## 1. Die Kostenrechnung, die über Erfolg oder Misserfolg entscheidet

Eine Round-Trip-Position von 500 USD in einem Pool mit 25 000 USD Liquidität:

| Position | Kosten |
|---|---|
| Preisimpact Kauf (x·y=k) | ~196 bps |
| Preisimpact Verkauf | ~192 bps |
| Pool-Gebühr ×2 | 60 bps |
| Priority Fee / Gas | 10–40 bps |
| Adverse Selection (andere reagieren auf dasselbe Signal) | 50–150 bps |
| Token-Steuern (EVM, falls vorhanden) | 0–1000 bps |
| **Summe ohne Steuern** | **≈ 510–640 bps** |

**Über 5 % Round-Trip-Kosten.** Eine Strategie mit 55 % Trefferquote und
symmetrischen Auszahlungen verliert damit Geld. Deshalb ist die
EV-Berechnung in `scoring/scorer.py` die eigentliche Handelsentscheidung und
nicht der Score — und deshalb ist der Liquiditäts-Cap in `risk/sizing.py`
derjenige, der in der Praxis am häufigsten bindet.

Die naheliegende Reaktion — größere Positionen für bessere Kostenquoten —
funktioniert nicht: der Preisimpact wächst überproportional. Die tatsächliche
Kapazität dieser Strategie liegt bei wenigen hundert USD pro Position. Wer 50 000
USD einsetzen will, braucht andere Märkte.

## 2. Wo der Edge *nicht* liegt

**Nicht im Sniping neuer Launches.** Dieses Feld ist besetzt von Bots mit
co-lozierten Nodes, privaten Mempools und Jito-Bundle-Zugang; deren Latenzbudget
liegt im einstelligen Millisekundenbereich. Ein Python-Stack auf einem gemieteten
VPS liegt bei 400–3000 ms. Wer im ersten Block mitbietet, ist mit hoher
Wahrscheinlichkeit die Exit-Liquidität für genau diese Bots. Deshalb ist
`min_token_age_s` auf 180 gesetzt: das System tritt *bewusst* erst an, wenn die
Sniper-Phase vorbei ist.

**Nicht in besseren Indikatoren.** RSI und MACD auf einem 40 Minuten alten Token
mit 200 Trades haben nahe null Informationsgehalt. Sie stehen im Score, weil sie
angefragt waren und in der Aggregation nicht schaden — der Großteil des
tatsächlichen Signals kommt aus den Hard Gates, dem Smart-Money-Flow und der
Holder-Dynamik.

**Nicht in mehr Datenquellen.** Zwölf APIs, die dieselbe DexScreener-Zahl
weiterreichen, sind eine Quelle mit elf zusätzlichen Ausfallpunkten. Der Wert
liegt in *unabhängigen* Quellen: eigener RPC-Node gegen Aggregator, Simulation
gegen Scanner-API.

## 3. Wo der Edge plausibel liegt

- **Überlebensfilterung.** Der weit überwiegende Teil neuer Meme-Coins verliert
  binnen 24 Stunden fast die gesamte Liquidität. Ein Filter, der die
  offensichtlichen Rugs zuverlässig aussortiert, verbessert die Verteilung mehr
  als jedes Timing-Signal.
- **Disziplinierte Exits.** Die Renditeverteilung ist extrem rechtsschief. Wer
  die erste Leiterstufe konsequent verkauft, wandelt Positionen in kostenlose
  Optionen um. Die meisten Retail-Verluste entstehen beim Halten von Gewinnern
  bis auf null.
- **Strukturelle Exits.** LP-Abzug, Deployer-Bewegung, fehlschlagende
  Sell-Simulation — diese Signale kommen aus der Chain und nicht aus dem Chart
  und retten mehr Kapital als jeder Stop-Loss.

## 4. Warum der Backtest lügt

| Problem | Wirkung | Gegenmaßnahme im Code |
|---|---|---|
| Survivorship Bias | tote Token fehlen im Datensatz | `SurvivorshipAudit` verweigert unplausible Datensätze |
| Latenz | Fill zum Signalpreis statt 1 s später | `LatencyModel`, log-normal je Chain |
| Preisimpact | Fill zum Mid | `FillModel` gegen echte Pool-Reserven |
| Fehlgeschlagene Transaktionen | 100 % Fill-Rate angenommen | `base_failure_rate`, erhöht bei Congestion |
| Multiple Testing | bester von 300 Parametersätzen | `deflated_sharpe_ratio(n_trials=...)` |
| Look-Ahead | Feature nutzt spätere Daten | `as_of` als Pflichtargument |

Auch mit allen sechs Gegenmaßnahmen bleibt ein Backtest hier eine *Plausibilitäts-
prüfung*, keine Validierung. Die einzige belastbare Validierung ist Paper Trading
gegen Live-Daten — deshalb ist die 14-Tage-Shadow-Phase in `LiveExecutorGuard`
als Konstruktor-Invariante erzwungen und nicht als Empfehlung formuliert.

## 5. Bewusst nicht implementiert

Folgendes wurde angefragt oder liegt nahe, ist aber **nicht** Teil des Systems:

- **Sandwich-Angriffe / Frontrunning fremder Pending-Transaktionen.** Das ist
  Wertabschöpfung zulasten anderer Nutzer, nicht Alpha aus Prognose.
- **Wash Trading zur Volumensimulation.** Ist Marktmanipulation. Das System
  *erkennt* es (`volume_liquidity_sanity`) und handelt solche Token nicht.
- **Koordinierte Social-Kampagnen für gehaltene Token.** Pump-and-Dump.
- **Handel auf nicht-öffentlichen Informationen.** Das Smart-Money-Tracking nutzt
  ausschließlich die öffentliche On-Chain-Historie — bereits bestätigte, für
  jeden sichtbare Transaktionen.

Die Grenze ist nicht immer scharf, aber ein brauchbarer Test lautet: *Beruht der
Gewinn auf einer besseren Prognose oder darauf, dass jemand anders schlechter
ausgeführt wird, weil ich es so eingerichtet habe?* Nur Ersteres ist hier
implementiert.

## 6. Regulatorik und Steuern

Je nach Wohnsitz können automatisierter Handel, gewerbliche Einstufung,
Meldepflichten und die steuerliche Behandlung jedes einzelnen Swaps relevant
sein. In Deutschland und Österreich ist z. B. jeder Krypto-Krypto-Tausch ein
Realisationsvorgang — ein Bot mit 40 Trades pro Tag produziert entsprechend
Dokumentationsaufwand. Die `fills`-Tabelle ist append-only unter anderem deshalb.
Das ist keine Steuerberatung; kläre das vor dem Live-Betrieb mit jemandem, der
das darf.

## 7. Realistische Betriebsrisiken

- **RPC-Ausfall mitten in einer Position.** → Watchdog setzt `HALT`, Position
  Manager arbeitet mit Fallback-Endpoint weiter.
- **Datenquelle liefert still `null` statt Fehler.** → Gates schlagen fehl
  (fail closed), Rejection-Histogramm im Dashboard zeigt den Ausschlag.
- **Honeypot mit verzögerter Steuer.** Manche Contracts aktivieren die Sell-Tax
  erst nach N Verkäufen oder oberhalb einer Größe. → Sell-Simulation läuft in
  der Zielgröße und wird während der Haltedauer wiederholt.
- **Der Operator schaltet nach drei Verlusttagen die Limits hoch.** Das ist der
  statistisch wahrscheinlichste Weg zum Totalverlust. Deshalb erfordert `LOCKED`
  einen manuellen Unlock mit 2FA und schreibt einen Audit-Eintrag.

## 8. Empfohlenes Vorgehen

1. Backtest mit archivierten Daten inkl. toter Token — Ergebnis skeptisch lesen.
2. 14+ Tage Paper Trading gegen Live-Daten, ohne Konfigurationsänderung.
3. Vergleich: Paper-Verteilung gegen Backtest-Verteilung. Weichen sie stark ab,
   ist das Backtest-Setup falsch — nicht der Markt.
4. Live-Start mit einem Betrag, dessen Totalverlust folgenlos ist.
5. Erst nach 200+ Live-Trades über die Positionsgröße nachdenken.

Schritt 3 überspringt fast jeder. Er ist der wertvollste.
