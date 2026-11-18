# LIFE OS – Gesamtkonzept (Version 1.0, Entwurf zur Freigabe)

Stand: 18.11.2026 · Status: **wartet auf deine Zustimmung** – es wurde noch nichts programmiert.

---

## 0. Zusammenfassung in 10 Sätzen

1. LIFE OS ist eine Web-App (PWA), die du auf dem iPhone über Safari zum Home-Bildschirm hinzufügst und am PC im Browser nutzt.
2. Das Herzstück ist die **Heute-Übersicht**: Termine, die 3 wichtigsten Dinge, Fristen und „Wo war ich?“ auf einen Blick.
3. Alles landet zuerst in der **Universal Inbox** – tippen, diktieren, Text (z. B. Briefe) einfügen. Kein Nachdenken, wohin es gehört.
4. Eine **KI (Claude) sortiert vor**, du bestätigst mit einem Tipp. Nichts Wichtiges passiert ohne dich.
5. Dein **iCloud-Kalender bleibt dein Kalender** – LIFE OS liest und schreibt ihn über CalDAV mit einem App-spezifischen Passwort.
6. ADHS-Hilfen sind fest eingebaut: „Was jetzt?“, Überforderungsmodus, Wiedereinstieg, Aufgaben zerlegen, Energie-Abfrage.
7. Später kommen Gmail, Finanzen, Dokumente, Einkauf, Projekte/Ziele und Zusatzarbeit-Erfassung dazu – ohne Neubau.
8. Daten liegen **in der EU (Frankfurt)**, verschlüsselt übertragen, nur für dich zugänglich.
9. Laufende Kosten: **ca. 0–3 € pro Monat**, mit hartem KI-Limit bei 5 €.
10. Alles Wichtige funktioniert **auch ohne KI**.

---

## 1. Produktbeschreibung

**LIFE OS** ist dein persönliches Lebens-Betriebssystem: ein Ort für alles, was du im Kopf hast, mit einem Assistenten, der vorsortiert und dir sagt, was *jetzt* sinnvoll ist.

**Für wen gebaut (aus deinen Antworten):**

| Was du gesagt hast | Was LIFE OS daraus macht |
|---|---|
| Ich vergesse Dinge | Fristen-Radar, sanfte Push-Erinnerungen, „Nicht vergessen“-Bereich in Heute |
| Zu viel offen | Inbox statt 10 Listen, „Was jetzt?“ zeigt max. 3 Dinge, Wochen-Check |
| Ich fange nicht an | Aufgaben in Mini-Schritte zerlegen, Energie-passende Vorschläge |
| Alles verstreut | Ein System, eine Suche über alles, iCloud + Gmail integriert |
| Hauptjob + fast täglich Zusatzarbeit | Realistische Tagesplanung mit wenig Abendkapazität, Zusatzarbeit-Erfassung |
| Den ganzen Tag vereinzelt Gedanken | Erfassung in < 3 Sekunden, „Wo war ich?“-Notiz |

**Leitsatz:** *Nicht mehr Funktionen – weniger Kopf-Last.*

---

## 2. Funktionslandkarte

Legende: ✅ von dir gewählt · 💡 mein Vorschlag · Stufe: **V1** = erste Version, **später** = geplante Erweiterung, **Idee** = Zukunft

### 2.1 Unverzichtbar (Version 1)

| Funktion | Was sie macht | KI nötig? | Kosten |
|---|---|---|---|
| ✅ Heute-Übersicht | Termine, Top 3, Fristen, „Wo war ich?“, Energie | nein | 0 € |
| ✅ Universal Inbox | Alles rein: Text, Diktat, eingefügte Briefe | nein | 0 € |
| ✅ KI-Inbox-Sortierung | Macht aus „zahnarzt anrufen bis fr“ eine Aufgabe mit Frist | ja | Bruchteile von Cent |
| ✅ Aufgaben | Unteraufgaben, Fristen, Priorität, Dauer, Energiebedarf, Wiederholung | nein | 0 € |
| ✅ Notizen | Schnell + lang, Tags, Checklisten, Volltextsuche | nein | 0 € |
| ✅ Kalender intern + iCloud lesen/schreiben | Tag/Woche/Monat, Zeitblöcke | nein | 0 € |
| ✅ „Was jetzt?“-Knopf | Max. 3 Vorschläge nach Frist, Zeit, Energie | Basis ohne KI, Feinschliff mit KI | 0 € |
| ✅ Überforderungsmodus | Nur noch EIN nächster kleiner Schritt sichtbar | nein | 0 € |
| ✅ Energie-Abfrage | Ein Tipp: wenig / mittel / viel | nein | 0 € |
| 💡 „Wo war ich?“-Notiz | Ein Satz beim Unterbrechen, wird beim Wiederkommen gezeigt | nein | 0 € |
| ✅ Push-Erinnerungen | Sanft, selten, freundlich formuliert | nein | 0 € |
| 💡 Suche über alles | Ein Feld findet Aufgaben, Notizen, Termine (später Mails, Dokumente, Ausgaben) | nein | 0 € |

### 2.2 Sinnvolle Erweiterungen (direkt danach)

| Funktion | KI? | Stufe |
|---|---|---|
| ✅ Aufgaben zerlegen („Steuererklärung“ → 6 Mini-Schritte) | ja | V1.1 |
| ✅ Wiedereinstiegsmodus | ja | V1.1 |
| 💡 Brief-Assistent (Was wollen die? Bis wann? Was tun?) | ja | V1.1 |
| 💡 Fristen-Radar (nächste 30 Tage aus allen Quellen) | nein | V1.1 |
| 💡 Abends Kopf leeren (2-Min-Dialog vor dem Schlafen) | optional | V1.1 |
| 💡 Wochen-Check (sonntags, 10 Min, geführt) | optional | V1.1 |
| ✅ Realistische Tagesplanung / Überlastungs-Warnung | ja | V1.1 |
| ✅ Gmail lesen: wichtige Mails, Aufgaben & Fristen erkennen | ja | später (Phase 5) |
| ✅ Finanzen (manuell + CSV-Import, Budgets, Fixkosten) | optional | später |
| ✅ Dokumente (Briefe, Rechnungen, Verträge ablegen) | optional | später |
| ✅ Haushalt & Einkaufsliste | nein | später |
| ✅ Ziele & Projekte | nein | später |
| 💡 Zusatzarbeit erfassen (Stunden, Einnahmen, Monatsübersicht) | nein | später |

### 2.3 Optionale Komfortfunktionen

- 💡 **iPhone-Kurzbefehl „LIFE OS notieren“** – „Hey Siri, LIFE OS“ → diktieren → landet in der Inbox. *Kostenlos, sehr ADHS-freundlich, funktioniert auch bei gesperrtem Handy.*
- 💡 **Teilen-Menü** – Text/Screenshot aus anderen Apps an LIFE OS schicken (über denselben Kurzbefehl).
- Antwortentwürfe für E-Mails (nur Entwurf, nie automatisch senden).
- Routinen (Morgen/Abend) ohne Streaks und Schuldgefühle.
- Fokus-Timer (hattest du nicht gewählt – bleibt optional).

### 2.4 Zukunftsideen (in der Architektur vorbereitet, nicht geplant)

Abos & Kündigungsfristen · Verträge & Versicherungen · Kontakte & Geburtstage · Gesundheit/Sport · Reisen · Fahrzeug · Bankanbindung (nur lesend, nur mit separater Prüfung) · semantische Suche („Was hat der Vermieter letztes Jahr wegen der Heizung geschrieben?“) · Foto-Erkennung von Briefen.

---

## 3. Modulstruktur

```
LIFE OS
├── Kern (immer da)
│   ├── Erfassung (Inbox, Diktat, Kurzbefehl)
│   ├── Einträge & Verknüpfungen (alles kann mit allem verbunden sein)
│   ├── Suche
│   ├── Erinnerungen & Push
│   ├── KI-Vorschläge (mit Freigabe-Status)
│   └── Protokoll („Was hat sich geändert, durch wen?“)
├── Module (einzeln an-/abschaltbar)
│   ├── Heute · Aufgaben · Notizen · Kalender          ← V1
│   ├── Projekte & Ziele · Einkauf & Haushalt          ← später
│   ├── Finanzen · Zusatzarbeit · Dokumente            ← später
│   └── E-Mail (Gmail)                                 ← später
└── Integrationen (Adapter, austauschbar)
    ├── iCloud-Kalender (CalDAV)                       ← V1
    ├── KI-Anbieter (Claude, austauschbar)             ← V1
    └── Gmail API                                      ← später
```

**Warum so?** Jedes Modul nutzt dieselbe Grundlage (Einträge, Verknüpfungen, Erinnerungen, Suche). Ein neues Modul wird *angesteckt*, nicht neu gebaut.

---

## 4. Technische Architektur

### 4.1 Empfehlung

| Baustein | Wahl | Warum | Kosten |
|---|---|---|---|
| App (Oberfläche + Server-Logik) | **Next.js + TypeScript + React + Tailwind CSS** | Standard, sehr gut wartbar, PWA-fähig | 0 € |
| Hosting | **Vercel (Hobby-Tarif)** | Ein Klick-Deployment, HTTPS automatisch, für private Nutzung kostenlos | 0 € |
| Datenbank, Login, Dateien | **Supabase (Free, Region Frankfurt)** | PostgreSQL + Anmeldung + Zugriffsschutz (Row Level Security) + Dateispeicher in einem | 0 € |
| Hintergrund-Jobs (Kalender-Sync, Erinnerungen) | **Supabase Cron + Edge Functions** | Läuft auch, wenn die App geschlossen ist | 0 € |
| Push | **Web Push (Standard, VAPID)** | Funktioniert auf iPhone ab iOS 16.4 für Home-Bildschirm-Apps | 0 € |
| KI | **Claude API** – Haiku 5.5 für Einfaches, Sonnet 5.5 für Planung | Sehr günstig, gute deutsche Sprache, austauschbar | ca. 0,50–3 €/Monat |
| Quellcode | GitHub (privat) | Versionierung, Backup des Codes | 0 € |

### 4.2 Verglichene Alternativen (kurz)

| Alternative | Warum nicht als erste Wahl |
|---|---|
| Firebase (Google) | Keine echte SQL-Datenbank, Daten-Verknüpfungen umständlicher, Zugriffsregeln schwerer verständlich |
| Eigener Server (Hetzner VPS, ca. 4–5 €/Monat) | Günstig und EU, aber du müsstest Updates/Sicherheit selbst verantworten – zu viel Wartung |
| Cloudflare Pages + D1 | Sehr günstig, aber Login und Dateien müsste man zusammenstückeln |
| Native iOS-App | Nicht möglich ohne Mac – und nicht nötig |

### 4.3 Wie es zusammenspielt

```
 iPhone (Safari/Home-Bildschirm)      Windows-PC (Browser)
            \                              /
             \       HTTPS (verschlüsselt)/
              ▼                          ▼
          ┌──────────── Vercel ─────────────┐
          │  Next.js App (Oberfläche + API) │──► Claude API (nur nötige Ausschnitte)
          └───────────────┬─────────────────┘
                          │
          ┌────── Supabase (Frankfurt) ─────┐
          │ Datenbank · Login · Dateien     │
          │ Cron-Jobs ─► iCloud CalDAV      │
          │           ─► Push an dein iPhone│
          └─────────────────────────────────┘
```

**Wichtig:** Claude Code ist nur die Werkstatt. Nach der Veröffentlichung läuft LIFE OS komplett selbstständig auf Vercel + Supabase.

### 4.4 Wichtige iPhone-Besonderheiten (ehrlich benannt)

1. **Anmelde-Link öffnet sich in Safari, nicht in der Home-Bildschirm-App.** Auf dem iPhone haben Home-Bildschirm-Apps einen eigenen Speicher. Ein klassischer Magic Link würde dich in Safari einloggen, nicht in der App. **Lösung:** Du bekommst per E-Mail zusätzlich einen **6-stelligen Code**, den du in der App eintippst. Danach bleibst du dauerhaft angemeldet. Später optional: Passkey (Face ID).
2. **Push nur nach „Zum Home-Bildschirm“** und einmaliger Erlaubnis.
3. **Offline:** Die App öffnet sich offline und zeigt den letzten Stand; Inbox-Eingaben werden zwischengespeichert und beim nächsten Netz synchronisiert.

---

## 5. Datenmodell (vereinfacht)

Alle Tabellen haben `user_id` → Zugriffsschutz pro Person (Row Level Security), `created_at`, `updated_at`.

| Tabelle | Inhalt |
|---|---|
| `inbox_items` | Roh-Eingaben (Text, Quelle: Tippen/Diktat/Kurzbefehl/Mail), Status: neu / verarbeitet / verworfen |
| `tasks` | Titel, Notiz, Status, Priorität, Frist, geplantes Datum, Dauer (geschätzt/tatsächlich), Energie (wenig/mittel/viel), Wiederholung, `parent_id` (Unteraufgaben), Projekt, Bereich |
| `notes` | Titel, Inhalt (Markdown), Typ (Notiz/Idee/Checkliste/Dokumentation) |
| `events` | Termine & Zeitblöcke: Start, Ende, Ort, Quelle (LIFE OS / iCloud), externe ID, ETag |
| `calendar_accounts` | iCloud-Verbindung: Benutzername, **verschlüsseltes** App-Passwort, ausgewählte Kalender, Sync-Token |
| `areas` | Lebensbereiche (Arbeit, Zusatzarbeit, Haushalt, Finanzen, …) |
| `projects` / `goals` | Projekte, Ziele, Meilensteine (später) |
| `tags` + `entity_tags` | Schlagworte für alles |
| `links` | **Verknüpfung zwischen beliebigen Einträgen** (Aufgabe ↔ Notiz ↔ Termin ↔ Mail ↔ Dokument) |
| `reminders` | Wann, was, Kanal (Push), Status (geplant/gesendet) |
| `ai_suggestions` | Was die KI vorschlägt + Status: **Information → Vorschlag → geplant → bestätigt → ausgeführt** (oder abgelehnt) |
| `activity_log` | Jede Änderung: was, wann, durch wen (du / KI-Vorschlag bestätigt / Automatik / Sync) |
| `ai_usage` | Jeder KI-Aufruf: Zweck, Modell, Tokens, Kosten → Kostenanzeige und Limit |
| `energy_checkins` | Energielevel mit Zeitpunkt |
| `resume_notes` | „Wo war ich?“-Sätze |
| `push_subscriptions` | Deine Geräte für Push |
| `settings` | Module an/aus, Ruhezeiten, KI-Limit, Automatisierungen an/aus |
| später: `emails`, `documents`, `transactions`, `accounts`, `budgets`, `subscriptions`, `shopping_items`, `work_sessions` (Zusatzarbeit), `contacts` |

**Suche:** PostgreSQL-Volltextsuche mit deutscher Sprachunterstützung – kostenlos, schnell, ohne KI.

---

## 6. Navigationskonzept

**iPhone – Leiste unten (vorläufig, später gemeinsam testen und anpassen):**

```
[ Heute ]  [ Kalender ]  [ ＋ ]  [ Aufgaben ]  [ Mehr ]
```

- **＋ (Mitte, groß):** Erfassen – öffnet sofort die Tastatur. Diktieren über die Mikrofon-Taste.
- **Heute:** Startseite. Oben: Energie-Tipp + „Was jetzt?“-Knopf. Darunter: Termine, Top 3, Fristen, Inbox-Zähler („5 Dinge warten aufs Sortieren“).
- **Mehr:** Inbox · Notizen · Suche · (später) Projekte, Finanzen, Mails, Dokumente, Einkauf · Einstellungen · **Datenschutz-Cockpit**.
- **PC:** Gleiche Bereiche als Seitenleiste links, mehr Platz für Wochenansicht.

---

## 7. Designkonzept

- **Ruhig:** viel Weißraum, eine Akzentfarbe (gedämpftes Blau-Violett), sanfte Karten mit runden Ecken.
- **Hell/Dunkel automatisch** nach iPhone-Einstellung.
- **Maximal 3 wichtige Dinge** pro Bereich sichtbar, Rest eingeklappt („+12 weitere“).
- **Keine roten Alarm-Wände:** Überfälliges heißt „Wartet noch“ in neutraler Farbe; wirklich Kritisches dezent orange.
- **Freundliche Sprache:** „Neuer Tag, neuer Anfang – sollen wir kurz aufräumen?“ statt „17 Aufgaben überfällig!“
- **Große Tippflächen** (mind. 44 px), Daumen-Zone unten.
- **Überforderungsmodus:** Ein Bildschirm, eine Karte, ein Knopf („Erledigt“ / „Kleiner machen“ / „Später“).

---

## 8. Integrationsstrategie

### 8.1 iCloud-Kalender (CalDAV) – Version 1

**Wie es funktioniert:**
1. Du erstellst bei Apple ein **App-spezifisches Passwort** (appleid.apple.com → Anmeldung & Sicherheit). Dein normales Apple-Passwort wird **nie** eingegeben oder gespeichert. Das App-Passwort kannst du jederzeit bei Apple widerrufen.
2. LIFE OS speichert es **verschlüsselt** (AES-256-GCM, Schlüssel nur auf dem Server, nicht in der Datenbank).
3. **Lesen:** Alle 15 Minuten im Hintergrund + beim Öffnen der App. Es werden nur Änderungen abgeholt (Sync-Token).
4. **Schreiben:** Neue Termine/Zeitblöcke aus LIFE OS → nach deiner Bestätigung in iCloud geschrieben.
5. **Konflikte:** Wurde ein Termin gleichzeitig am iPhone und in LIFE OS geändert, gewinnt **nichts automatisch** – LIFE OS zeigt beide Versionen und du wählst.
6. **Sync-Protokoll** in den Einstellungen: was wann gelesen/geschrieben wurde, Fehler im Klartext.

**Einschränkungen (ehrlich):**
- CalDAV ist von Apple nicht offiziell für Drittentwickler dokumentiert, funktioniert aber seit Jahren stabil (nutzen z. B. Thunderbird, DAVx⁵, Fantastical).
- Änderungen erscheinen mit bis zu 15 Min. Verzögerung in LIFE OS (am iPhone sofort, wenn du in LIFE OS änderst).
- **Apple Erinnerungen sind NICHT über CalDAV erreichbar** (Apple hat das Format 2019 geändert). Für deinen Bestand: einmaliger Import über einen iPhone-Kurzbefehl, den ich dir vorbereite – wenn du willst.

### 8.2 KI (Claude)

- **Austauschbarer Adapter**: Anbieter/Modell per Einstellung wechselbar.
- **Modellwahl je Aufgabe:** Haiku 5.5 (Sortieren, Fristen erkennen, Zerlegen) · Sonnet 5.5 (Tages-/Wochenplanung, Wiedereinstieg).
- **Nur auf Anlass:** beim Sortieren der Inbox, auf Knopfdruck, 1× morgens für den Tagesvorschlag (abschaltbar). Keine Dauer-Analyse.
- **Datensparsam:** Es wird nur geschickt, was für die Aufgabe nötig ist (z. B. Titel + Fristen offener Aufgaben, keine kompletten Notizen).
- **Ohne KI:** „Was jetzt?“ funktioniert mit festen Regeln (Frist × Priorität × Energie × freie Zeit). Die KI ist ein Plus, keine Voraussetzung.

### 8.3 Gmail – Phase 5

- Offizielle Gmail API mit OAuth 2.0, Start mit **nur Lesen** (`gmail.readonly`).
- **Wichtig zu wissen:** Lese-Zugriff auf Gmail ist bei Google eine „eingeschränkte Berechtigung“. Für eine öffentliche App wäre eine teure Sicherheitsprüfung nötig. **Für deine private Nutzung** registrieren wir ein eigenes Google-Cloud-Projekt nur für dich: Google zeigt beim Verbinden einmal den Hinweis „App nicht verifiziert“, den du bestätigst. Das ist für Ein-Personen-Nutzung vorgesehen und kostenlos.
- Senden/Löschen/Verschieben: erst später, jeweils mit eigener Berechtigung und **immer** mit deiner Freigabe pro Mail.

---

## 9. Sicherheitskonzept

| Maßnahme | Umsetzung |
|---|---|
| Verschlüsselte Übertragung | HTTPS überall (automatisch durch Vercel/Supabase) |
| Anmeldung | E-Mail-Code (kein Passwort), Sitzung auf deinen Geräten, Abmelden aller Geräte möglich |
| Zugriffsschutz | Row Level Security: Die Datenbank selbst verweigert Zugriff auf fremde Daten |
| Sensible Zugangsdaten (iCloud-App-Passwort, Gmail-Token) | Zusätzlich verschlüsselt (AES-256-GCM), Schlüssel nur als Server-Umgebungsvariable |
| API-Schlüssel | Nur in Vercel/Supabase-Einstellungen, **nie im Quellcode**, nie im Browser |
| Aktionen mit Folgen | Freigabe-Pflicht + Protokoll (E-Mail senden, externe Termine ändern/löschen, Daten endgültig löschen, Dienste verbinden) |
| Löschen | Erst „Papierkorb“ (30 Tage), endgültig nur mit Bestätigung |
| Backups | Nächtliches, verschlüsseltes Datenbank-Backup per GitHub Action (kostenlos) + jederzeit Export als JSON/CSV in der App |
| Missbrauchsschutz | Rate-Limits auf Login und KI, KI-Kostenlimit pro Monat |

**Ehrlicher Hinweis:** Der kostenlose Supabase-Tarif enthält keine automatischen Backups und pausiert Projekte nach 7 Tagen ohne Nutzung. Bei täglicher Nutzung kein Problem; das eigene nächtliche Backup schließt die Lücke.

---

## 10. Datenschutzkonzept (DSGVO)

**Datenschutz-Cockpit in der App** zeigt jederzeit:

| Wo | Welche Daten | Standort |
|---|---|---|
| Supabase | Alle deine LIFE-OS-Daten | EU (Frankfurt) |
| Vercel | Keine dauerhafte Speicherung, nur Durchleitung | EU-Region wählbar |
| Anthropic (Claude) | Nur die Ausschnitte, die du gerade sortieren/planen lässt | USA (Auftragsverarbeitung; API-Daten werden laut Anbieter standardmäßig nicht zum Training verwendet) |
| Apple (iCloud) | Kalendertermine – die liegen dort ohnehin | Apple |
| Google (Gmail, später) | Lesezugriff auf Mails – die liegen dort ohnehin | Google |

- **KI-Transparenz:** Jeder KI-Aufruf wird protokolliert (Zweck, gesendete Datenart, Kosten). Du kannst den Inhalt jeder Anfrage ansehen.
- **Rein private Nutzung** fällt unter die „Haushaltsausnahme“ der DSGVO – du bist nicht zu Datenschutzerklärungen o. Ä. verpflichtet. Trotzdem halten wir uns an die Grundsätze: Datensparsamkeit, Zweckbindung, Export, Löschung.
- **Achtung bei Daten Dritter:** Mails/Termine anderer Personen gehen nur im nötigen Umfang an die KI.
- Komplett-Export und Komplett-Löschung mit einem Knopf in den Einstellungen.

---

## 11. Kostenübersicht

| Posten | Monatlich |
|---|---|
| Vercel Hobby | 0 € |
| Supabase Free (500 MB DB, 1 GB Dateien) | 0 € |
| Web Push, Cron, Backups (GitHub Actions) | 0 € |
| Webadresse `xyz.vercel.app` | 0 € |
| KI (Claude API, Prepaid-Guthaben, hartes Limit) | **ca. 0,50–3 €** (Limit: 5 €) |
| **Summe** | **ca. 0,50–3 €** |

**Beispielrechnung KI:** Eine Inbox-Sortierung mit Haiku 5.5 kostet ca. 0,03 Cent. 30 Eingaben pro Tag ≈ 0,30 €/Monat. Eine Tagesplanung mit Sonnet 5.5 ca. 2 Cent → 30 €-Cent/Monat für tägliche Planung. Zusammen realistisch unter 2 €.

**Optional später (nur nach deiner Zustimmung):** Eigene Domain (ca. 1 €/Monat) · Supabase Pro (25 $/Monat, nur bei sehr vielen Dokumenten – vorher Alternative Cloudflare R2: 10 GB kostenlos).

**Was du einmalig anlegen musst** (ich führe dich Schritt für Schritt): GitHub-Konto · Vercel-Konto · Supabase-Konto · Anthropic-Konto mit ca. 5 € Startguthaben · App-spezifisches Apple-Passwort.

---

## 12. Entwicklungsphasen & Priorisierung

| Phase | Inhalt | Ergebnis für dich |
|---|---|---|
| **1 – Konzept** | Dieses Dokument | ✅ erledigt, wartet auf Freigabe |
| **2 – Fundament** | Projekt, Datenbank, Login per Code, Design-Grundlage, Navigation, PWA, Deployment | Du kannst LIFE OS auf dem iPhone installieren und dich anmelden |
| **3 – Core (V1)** | Heute, Inbox, Aufgaben, Notizen, interner Kalender, Suche, „Was jetzt?“ (regelbasiert), Überforderungsmodus, Energie, „Wo war ich?“, Push | Du kannst LIFE OS im Alltag nutzen |
| **3b – iCloud (V1)** | CalDAV lesen + schreiben, Konfliktanzeige, Sync-Protokoll | Deine echten Termine in Heute |
| **4a – KI-Basis (V1)** | Inbox-Sortierung, KI-Kostenanzeige + Limit | Stichworte werden zu sauberen Aufgaben |
| **4b – KI-Assistent (V1.1)** | Aufgaben zerlegen, Brief-Assistent, Tagesplanung + Überlastungswarnung, Wiedereinstieg, Wochen-Check, Abends Kopf leeren, Fristen-Radar | Der Assistent denkt mit |
| **5 – Gmail** | Lesen, wichtige Mails, Aufgaben/Fristen aus Mails | Mails gehen nicht mehr unter |
| **6 – Lebensbereiche** | Finanzen (CSV), Zusatzarbeit, Dokumente, Einkauf, Projekte & Ziele | Alles an einem Ort |
| **7 – Automatisierungen** | Einzeln schaltbare Workflows mit Protokoll, Abos/Verträge | LIFE OS nimmt Arbeit ab |

**Arbeitsweise:** Kleine Schritte. Nach jedem Schritt zeige ich dir, was funktioniert und was noch fehlt – mit Link zum Ausprobieren.

---

## 13. Offene Entscheidungen

1. **Eigenes Repository für LIFE OS?** Das aktuelle Repository `mca` enthält ein anderes Projekt (Trading-System). Empfehlung: neues privates Repository `life-os`.
2. **Import von Apple Erinnerungen/Notizen** – später entscheiden (Kurzbefehl wird vorbereitet).
3. **Navigation** – vorläufig wie oben, nach 1–2 Wochen Nutzung gemeinsam anpassen.
