# Überblick: Was ist das hier eigentlich?

Diese Datei beantwortet zwei Fragen: **wie sieht das Ganze aus**, wenn man
einen Schritt zurücktritt, und **was tut jede Datei**, in einem Satz.

---

## "Wann packst du das als eins zusammen?" — kurze Antwort

Es ist bereits so gepackt, wie es bei einem System dieser Art überhaupt geht:
**ein Kommando** (`./start.sh`) startet **alle Programme, die zusammen laufen
müssen**. Es gibt keine Ein-Datei-Version davon — aus demselben Grund, aus dem
es bei einer Trading-Firma, einer Banking-App oder Netflix auch keine gibt:

Das System muss mehrere Dinge **gleichzeitig, dauerhaft und unabhängig
voneinander** tun — der Blockchain zuhören, während gleichzeitig eine offene
Position überwacht wird, während gleichzeitig ein Kauf ausgeführt wird. Presst
man das in eine Datei/einen Prozess, reißt ein Absturz in der Kursberechnung
den Teil mit, der gerade Geld verwaltet. Die Trennung in ~15 kleine Programme
ist der Sicherheitsmechanismus, nicht Bürokratie — deckt sich mit Punkt 4 in
der README (Live-Handel als Konstruktor-Invariante): der Executor-Prozess ist
bewusst der *einzige*, der je einen Schlüssel sieht.

`docker-compose.yml` ist das Rezept, das festlegt, welche Programme das sind
und wie sie verbunden werden. `start.sh` ist der eine Tastendruck, der dieses
Rezept ausführt. Das *ist* die Verpackung.

---

## Das Gesamtbild

```mermaid
flowchart LR
    subgraph W["✅ Beobachten"]
        ING["ingest/<br/>hört der Blockchain zu"]
    end
    subgraph M["✅ Prüfen & Bewerten"]
        ENR["enrich/<br/>ist der Coin sicher?"]
        SCO["scoring/<br/>Note 0–100 + Kauf-Empfehlung"]
    end
    subgraph E["✅ Entscheiden & Handeln"]
        RG["risk/<br/>darf ich das kaufen?"]
        EX["execution/<br/>kauft/verkauft wirklich"]
        PM["portfolio/<br/>wann verkaufen?"]
    end
    subgraph O["✅ Aufpassen"]
        WD["ops/watchdog"]
        SCH["ops/tasks"]
    end
    DB[("db/<br/>Gedächtnis")]

    ING --> ENR --> SCO --> RG --> EX --> PM
    PM -. Exit-Signal .-> EX
    RG & EX & PM <--> DB
    WD -. überwacht .-> RG & EX & PM
```

Pfeile = Nachrichten über eine Warteschlange (Redis). Jede Box ist ein
eigenständiges Programm, das `docker compose up` startet und das sich neu
starten lässt, ohne die anderen mitzureißen.

---

## Jeder Ordner in einem Satz

| Ordner | Tut was | Status |
|---|---|---|
| `domain/` | Definiert die Grundbegriffe — Position, Trade, Coin — auf denen alles andere aufbaut | ✅ |
| `ingest/` | Hört der Solana-Blockchain zu, meldet neue Liquiditätspools | ✅ nur Solana |
| `enrich/` | Prüft einen gemeldeten Coin: Mint-Rechte weg? Wale (Pool selbst ausgeschlossen)? Liquidität/Volumen via DexScreener | ✅ nur Solana, kein Social/Whale-Flow |
| `scoring/` | Rechnet aus den Prüfungen eine Bewertung + EV-Entscheidung, mit selbst-aufwärmendem Vergleichs-Cohort | ✅ Trefferwahrscheinlichkeit bleibt neutral bis ein Modell trainiert ist |
| `risk/` | Sagt Ja/Nein zum Kauf, abhängig vom Gesamtrisiko (Tagesverlust, offene Positionen, Circuit Breaker) | ✅ |
| `execution/` | Führt Kauf/Verkauf auf der Blockchain aus, verwaltet den Schlüssel | ✅ nur Solana |
| `portfolio/` | Verwaltet offene Positionen, entscheidet: jetzt (teilweise) verkaufen? | ✅ |
| `ops/` | Tägliche Wartung, Notfall-Stopp bei Ausfällen, Wallet-Sweep-Erkennung | ✅ |
| `db/` | Datenbank-Anbindung — das Gedächtnis zwischen Neustarts | ✅ Code, 🚧 nie gegen echtes Postgres getestet |
| `api/`, `dashboard.jsx` | Was du im Browser siehst und anklickst | 🚧 Grundgerüst |
| `backtest/`, `ml/` | Historische Simulation + Trefferwahrscheinlichkeits-Modell | ✅ Logik, 🚧 kein Trainingsdatensatz |
| `alerts/` | Telegram-Benachrichtigungen bei wichtigen Ereignissen | ✅ |
| `tests/` | 158 automatische Prüfungen, laufen bei jeder Änderung | ✅ |

---

## Was passiert HEUTE wirklich, wenn du `./start.sh` ausführst?

1. Alle Programme starten — Ingest, Enrich, Scorer, Risk Gate, Executor,
  Position Manager, Watchdog, API, Dashboard.
2. Der Ingest-Prozess sieht neue Solana-Pools in Echtzeit.
3. Enrich prüft jeden gemeldeten Pool: Mint-Rechte weg? Liquidität hoch genug?
  Wale? Score und Risk Gate entscheiden dann wirklich Ja/Nein.
4. **Trotzdem wird faktisch nichts gekauft** — nicht weil ein Teil fehlt,
  sondern weil `should_trade()` eine kalibrierte Trefferwahrscheinlichkeit
  verlangt, und die kommt aus einem ML-Modell, das erst nach genug
  abgeschlossenen Trades trainierbar ist. Ein frisch gestarteter Bot hat noch
  keine — also lehnt jedes Signal ehrlich mit „no calibrated win probability"
  ab, statt zu raten.

Der Weg vom „neuer Pool" zur „Kauf-Entscheidung" ist damit komplett — was
fehlt, ist Erfahrung, kein Code. Das behebt sich von selbst, sobald über
Paper-Trading genug abgeschlossene Positionen für das erste Modelltraining
zusammenkommen (`ops/tasks.py`s `retrain_model_task`, täglich um 3 Uhr UTC).

---

## Wenn du selbst reinschauen willst — diese Reihenfolge ergibt ein Bild

1. **Diese Datei** — die Landkarte
2. `domain/models.py` — die Grundbegriffe (kein Import von irgendwo sonst)
3. `scoring/gates.py` + `scoring/scorer.py` — wie eine Kauf-Entscheidung
  fachlich zustande kommt (unabhängig von Blockchain-Details lesbar)
4. `risk/gate_service.py` → `execution/worker.py` → `portfolio/manager.py` —
  der Live-Pfad, in der Reihenfolge, in der eine Nachricht ihn durchläuft
5. `docs/ARCHITECTURE.md` — die volle technische Tiefe mit Begründungen
  (Hot-Path/Cold-Path-Trennung, warum genau ein Risk-Gate-Prozess, etc.)
6. `README.md` — Setup, Testergebnisse, jeder bisher gefundene Bug

Wenn eine Datei nach dieser Reihenfolge immer noch keinen Sinn ergibt: sag
mir welche — das ist entweder eine fehlende Erklärung, die ich nachliefern
sollte, oder eine Datei, die wirklich unnötig ist.
