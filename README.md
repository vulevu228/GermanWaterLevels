# Pegelstände in Deutschland

Dieses Projekt holt die **Wasserstand-Rohdaten** von **PEGELONLINE**. PEGELONLINE
ist ein Dienst der Wasserstraßen- und Schifffahrtsverwaltung des Bundes (WSV).
Ein Python-Skript lädt die Daten herunter, macht sie sauber und speichert sie in
einer Parquet-Datei. Mit dieser Datei kann man dann in **Power BI** eine Karte
und Diagramme bauen.

Die Werte sind Wasserstände in **Zentimeter über dem Pegelnullpunkt (PNP)**. Es
gibt für die meisten Pegel alle 15 Minuten einen Wert.

## Was das Projekt macht

1. Es liest die Ordner-Struktur auf der Webseite von PEGELONLINE.
2. Es lädt für jeden Pegel und jeden Tag eine kleine CSV-Datei herunter.
3. Es liest diese CSV-Dateien und schreibt alle Werte in eine große Tabelle.
4. Es holt zusätzlich die Koordinaten (Breite und Länge) für jeden Pegel. Damit
   kann Power BI eine Karte zeigen.

## Die Datenquelle

Adresse: <https://pegelonline.wsv.de/webservices/files/> → Ordner
**`Wasserstand Rohdaten`**. Man braucht **keinen Zugangsschlüssel** und **kein
Konto**.

Die Ordner sind vier Ebenen tief:

```
Wasserstand Rohdaten/
  RHEIN/                     ← Gewässer (etwa 90)
  ELBE/
    <Pegel-Nummer als UUID>/ ← Pegel (der Ordner-Name ist eine lange ID, kein Klartext)
      08.09.2026/            ← ein Ordner pro Tag, Name = TT.MM.JJJJ
        down.csv             ← diese Datei lesen wir
        down.txt             ← die gleichen Infos als Text
        down.zrxp            ← ein Fachformat (kein ZIP)
```

**Wichtig:** PEGELONLINE hält pro Pegel nur die **letzten ungefähr 80 Tage**.
Ältere Tage werden gelöscht. Darum muss das Skript **jeden Tag** laufen. So wird
die eigene Parquet-Datei mit der Zeit immer länger, auch wenn die Quelle kurz
bleibt.

## Was du nach dem Start siehst

Der Befehl `python fetch_pegelonline.py` macht zwei Schritte nacheinander:

**Schritt 1 – herunterladen (`mirror`).** Das Skript geht durch alle Gewässer und
Pegel. Für jeden Tag, den es noch nicht hat, lädt es die `down.csv` in den Ordner
`raw/`. Dateien, die schon da sind, überspringt es. Auf dem Bildschirm läuft so
etwas:

```
90 waterways (of 90)
[1/90] ALLER: 12 gauges
    running total: 340 new, 0 present, 0 failed
[2/90] DONAU: 25 gauges
    ...
mirror done: 740 new, 58600 present, 0 failed -> ...\raw
```

Beim ersten Mal dauert das lange (viele Tausend Dateien). Danach sind es pro Tag
nur noch rund 740 neue Dateien, also wenige Minuten.

**Schritt 2 – aufbereiten (`build`).** Das Skript liest die CSV-Dateien aus
`raw/`, das erste Mal alle, danach nur die neuen. Am Ende steht eine Zeile wie:

```
59354 raw files, 740 new to parse (58614 already in Parquet)
29.901.361 readings  |  737 gauges (731 with coords)  |  06.02.2024 - 09.09.2026  |  15.796 outliers nulled  |  0 broken files
-> ...\data\wasserstand.parquet
```

Danach kannst du `data/wasserstand.parquet` direkt in Power BI öffnen.

## Die zwei Ausgabe-Dateien

| Datei | Inhalt |
| --- | --- |
| `data/wasserstand.parquet` | die große Tabelle, eine Zeile pro Messwert. Spalten: `timestamp`, `waterway`, `station`, `station_no`, `pegel_uuid`, `parameter` (`W_O`), `unit` (`cm`), `value_cm`, `pnp_m`, `src_day`. Diese Datei ist rund 100 MB groß und wächst. Sie liegt **nicht** im Repo. |
| `data/pegel.parquet` | eine Zeile pro Pegel: Nummer, Name, Gewässer, `latitude`, `longitude`, `river_km`, `agency`, `pnp_m`. Klein, liegt im Repo, ist die **Tabelle für die Karte** in Power BI. |

## Warum die CSV-Dateien schwierig sind

Eine `down.csv` sieht so aus:

```
"08.09.2026";"via donau";"DONAU";"ACHLEITEN";"10094006";"W_O";"cm";"XXX,XXX";"XX.XX.XXXX";"XX:XX";"PNP";"287,7"
"00:15";"244"
"00:30";"244"
...
"11:00";"XXX,XXX"
"24:00";"XXX,XXX"
```

Fünf Probleme, und wie das Skript sie löst:

| Problem | Grund | Lösung im Skript |
| --- | --- | --- |
| Umlaute sehen kaputt aus (`KÃ¶ln`) | die Datei ist **Latin-1**, nicht UTF-8 | `.decode("latin-1")` |
| alles steht in einer Spalte | **Semikolon** trennt die Felder, jedes Feld hat Anführungszeichen | `csv.reader(..., delimiter=";")` |
| `287,7` wird falsch gelesen | deutsches **Komma** ist der Dezimalpunkt, der Punkt trennt Tausender | `s.replace(".", "").replace(",", ".")` |
| die erste Zeile hat 12 Felder, die anderen nur 2 | Zeile 1 ist eine **Kopfzeile** mit Infos zum Pegel (Nummer, Einheit, Pegelnullpunkt). Die Platzhalter `XXX,XXX` gehören zu einem Feld, das leer bleibt | Zeile 1 getrennt auswerten |
| `XXX,XXX` in der Werte-Spalte | so schreibt PEGELONLINE einen **fehlenden Wert** | wird zu `NaN` |
| in den Zeilen steht kein Datum, und es gibt `24:00` | das Datum steht nur in Zeile 1 und im Ordner-Namen. `24:00` heißt Mitternacht am nächsten Tag | Zeitstempel aus Ordner-Name + `HH:MM` bauen; `24:00` wird automatisch zum nächsten Tag |

## Ausreißer

Die Rohdaten sind **nicht geprüft**. Ein paar Pegel senden Fehler-Codes (`99999`,
`100000`) oder messen auf einer anderen Höhe. So kommen Werte wie 5.000 cm oder
500.000 cm vor. Das Skript setzt jeden Wert außerhalb von **−500 bis 2.500 cm**
auf `NaN`. Die Zeile und die Uhrzeit bleiben, nur der Wert fehlt dann. Das
betrifft etwa **0,05 %** der Messwerte an rund 10 von 737 Pegeln.

## Das Skript

`fetch_pegelonline.py` hat zwei Teile:

- **`mirror()`** – lädt die Dateien nach `raw/`. Es merkt sich nichts extra:
  Wenn die Datei schon auf der Festplatte liegt, wird sie übersprungen. Darum
  kann man das Skript jederzeit abbrechen und neu starten.
- **`build()`** – liest die Dateien und schreibt die Parquet-Dateien. Es
  arbeitet **inkrementell**: Zeilen, die schon in der Parquet-Datei sind (erkannt
  an `pegel_uuid` + `src_day`), werden nicht noch einmal gelesen. Wenn man die
  Parquet-Datei löscht, baut das Skript alles neu.

Nur drei Bibliotheken: `requests`, `pandas`, `pyarrow`.

## Installieren und starten

```bash
pip install -r requirements.txt
python fetch_pegelonline.py                 # beides: mirror + build
```

Einzeln:

```bash
python fetch_pegelonline.py mirror          # nur herunterladen
python fetch_pegelonline.py build           # nur Parquet neu bauen
python fetch_pegelonline.py mirror DONAU    # nur ein Gewässer (zum Testen)
```

## Jeden Tag automatisch laufen lassen

Unter Windows macht das die **Aufgabenplanung** (Task Scheduler). In diesem Repo
liegt dazu `run_daily.bat`. Diese Datei wechselt in den Projekt-Ordner, startet
das Skript und schreibt die Ausgabe nach `logs/daily.log`.

Die Aufgabe wird so angelegt (PowerShell):

```powershell
$dir = "C:\Users\<name>\...\GermanWaterLevels"
$action  = New-ScheduledTaskAction -Execute "$dir\run_daily.bat" -WorkingDirectory $dir
$trigger = New-ScheduledTaskTrigger -Daily -At 8:00AM
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Hours 4)
Register-ScheduledTask -TaskName "GermanWaterLevels-daily" -Action $action -Trigger $trigger -Settings $settings
```

`-StartWhenAvailable` heißt: Wenn der PC um 8 Uhr aus war, läuft die Aufgabe
später nach. Prüfen kann man sie mit:

```powershell
Get-ScheduledTaskInfo -TaskName "GermanWaterLevels-daily"
```

## Karte und Report in Power BI

Der Power-BI-Report (`pegelstaende-DE.pbix`) ist noch in Arbeit. Der Plan:

- **Power Query** lädt beide Parquet-Dateien.
- **Stern-Schema:** `pegel.parquet` ist die Pegel-Tabelle, dazu eine
  Datums-Tabelle. Beide sind mit `wasserstand.parquet` verbunden.
- **Karte** über `latitude` / `longitude` aus `pegel.parquet`, ein Punkt pro
  Pegel, Farbe nach aktuellem Wasserstand.
- **Kennzahlen (DAX):** aktueller Stand, Änderung in 24 Stunden und 7 Tagen,
  Minimum / Maximum / Mittel im gewählten Zeitraum, Anteil fehlender Werte.

## Grenzen der Daten

- **Rohdaten sind roh.** Keine Qualitätsprüfung. Die geprüfte Reihe gibt es bei
  PEGELONLINE im Ordner `Wasserstand` (ohne „Rohdaten“).
- Die Ordner-Namen der Pegel sind lange IDs. Klartext-Name, Nummer und Gewässer
  stehen in der Kopfzeile jeder `down.csv`.
- `value_cm` ist der Stand über dem **Pegelnullpunkt**, nicht über Normalnull.
  Die PNP-Höhe steht je Pegel in `pnp_m`.
- Der Zeitraum hängt davon ab, seit wann das Skript läuft. Die Quelle liefert
  immer nur die letzten rund 80 Tage.
- Die Zeitstempel sind **lokale deutsche Zeit** (Europe/Berlin), bewusst ohne
  Zeitzonen-Objekt gespeichert.

## Lizenzen

- **Code:** MIT (siehe [LICENSE](LICENSE)). Der Code gehört mir und darf frei
  genutzt werden.
- **Daten in `data/`:** **DL-DE→Zero-2.0** –
  <https://www.govdata.de/dl-de/zero-2-0>. Das ist quasi Gemeinfreiheit, ohne
  Bedingungen. Quelle: Wasserstraßen- und Schifffahrtsverwaltung des Bundes
  (WSV) / PEGELONLINE. Die Daten wurden nur zusammengeführt und ins
  Parquet-Format gebracht. Das Projekt gehört nicht zur WSV und wird von ihr
  nicht unterstützt.
