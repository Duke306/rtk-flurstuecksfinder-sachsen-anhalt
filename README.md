<div align="center">

<img src="docs/hero.svg" alt="RTK Flurstücksfinder Sachsen-Anhalt" width="100%">

# RTK Flurstücksfinder Sachsen-Anhalt

**Ein kompakter Open-Source-RTK-Rover für Raspberry Pi + LC29H mit landesweiter Offline-ALKIS-Suche, Offlinekarte, Grenzpunktnavigation und Höhenmodus.**

[![Python](https://img.shields.io/badge/Python-3.11%2B-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![Raspberry Pi](https://img.shields.io/badge/Raspberry%20Pi-Zero%202%20W-C51A4A?logo=raspberrypi&logoColor=white)](https://www.raspberrypi.com/products/raspberry-pi-zero-2-w/)
[![RTK](https://img.shields.io/badge/GNSS-RTK%20FIX-22c55e)](#rtk-und-sapos)
[![Offline](https://img.shields.io/badge/Karte-Offline-2563eb)](#offline-karte)
[![License: MIT](https://img.shields.io/badge/Code-MIT-yellow.svg)](LICENSE)

</div>

> [!IMPORTANT]
> Das Projekt dient zum Auffinden und Visualisieren digitaler ALKIS-Flurstücksgeometrien. Es ersetzt **keine amtliche Grenzfeststellung, Abmarkung oder Vermessung**. Für rechtssichere Grenzfragen ist die zuständige Vermessungsstelle maßgeblich.

## Was das Projekt kann

- 📍 **RTK-Navigation** zu Flurstückseckpunkten mit Richtung, Distanz und Ost-/Nord-Abweichung
- 🗺️ **komplett offline nutzbare Karte** für Sachsen-Anhalt auf Basis von Geofabrik/OpenStreetMap
- 🧭 **automatische Flurstückserkennung** an der aktuellen RTK-Position
- 🔎 Auswahl über **Landkreis → Gemarkung → Flur → Flurstück**
- 🧱 Darstellung der originalen ALKIS-Eckpunkte als **P1, P2, P3 …**
- ➕ optionale Hilfspunkte **Z1, Z2, Z3 …** im Abstand von 2 m / 5 m / 10 m
- 👆 Flurstück und Zielpunkte direkt **auf der Karte antippen**
- 🌐 **SAPOS HEPS/NTRIP** für RTK-Korrekturdaten über Smartphone-Hotspot
- 📶 Live-Status für WLAN, Internet, SAPOS, RTCM, Satelliten, HDOP und RTK FIX/FLOAT
- 📏 **Höhenmodus v6** mit intuitivem Messablauf, relativer/absoluter Sollhöhe, Toleranzanzeige und Messqualitätsbewertung
- 📌 gespeicherte Höhenpunkte **H1, H2, H3 …** und **REF** direkt auf der Karte, anklickbar und ansteuerbar
- 💾 landesweite ALKIS-Datenbank lokal auf der SD-Karte; Internet wird draußen nur noch für SAPOS benötigt
- 📱 Smartphone als Display — kein Display am Raspberry Pi erforderlich

<img src="docs/ui-preview.svg" alt="UI Vorschau" width="100%">

## Hardware

Die Referenzkonfiguration ist bewusst günstig gehalten:

| Komponente | Empfehlung |
|---|---|
| Rechner | Raspberry Pi Zero 2 W |
| RTK-GNSS | Waveshare LC29H(DA) GPS/RTK HAT |
| Antenne | Dualband-GNSS-Antenne, möglichst auf Stab/Groundplane |
| Internet | Smartphone-Hotspot |
| Strom | USB-Powerbank |
| Mechanik | Vermessungs-/GNSS-Stab mit Dosenlibelle |

Der LC29H(DA) wird als **Rover** betrieben. Beim Waveshare-HAT gehören beide UART-Jumper für den Raspberry-Pi-GPIO-Betrieb auf **Position B**.

## Architektur

<img src="docs/architecture.svg" alt="Systemarchitektur" width="100%">

Der Raspberry Pi verbindet drei Welten:

1. **LC29H** liefert GNSS/NMEA und empfängt RTCM über UART.
2. **SAPOS HEPS** liefert Korrekturdaten über NTRIP und den Smartphone-Hotspot.
3. **Lokale Daten** liefern ALKIS-Flurstücke und die Offline-Basiskarte ohne Cloud-Abhängigkeit.

## Schnellstart

### 1. Raspberry Pi vorbereiten

Empfohlen: **Raspberry Pi OS Lite 64-bit**. SSH und WLAN bereits im Raspberry Pi Imager aktivieren.

UART aktivieren:

```bash
sudo raspi-config
```

Unter `Interface Options → Serial Port`:

- Login shell over serial: **No**
- Serial port hardware enabled: **Yes**

Danach neu starten.

### 2. Repository installieren

```bash
git clone https://github.com/Duke306/rtk-flurstuecksfinder-sachsen-anhalt.git
cd rtk-flurstuecksfinder-sachsen-anhalt
chmod +x install.sh
./install.sh
sudo reboot
```

Danach im Smartphone-Browser:

```text
http://rtk.local:5000
```

> Wenn `rtk.local` im Netz nicht aufgelöst wird, die IP des Pi mit `hostname -I` anzeigen und `http://<IP>:5000` öffnen.

## Aktualisierung auf 6.1.1

Version 6.1.1 unterstützt auch ältere lokal installierte Datenbankhelfer und ergänzt eine nordorientierte Minikarte in der Navigation. Die Zielrichtung ist eine Richtung gegenüber Norden; der Bewegungspfeil ist kein Gerätekompass.

Im bestehenden Git-Checkout auf dem Raspberry Pi als Benutzer `pi` ausführen:

```bash
git pull --ff-only origin main
bash install.sh
```

Das Installationsskript kopiert **app.py und parcel_db.py gemeinsam** nach `/home/pi/rtk-rover` und startet den Dienst neu. Vorhandene ALKIS-/MBTiles-Dateien, Höhenpunkte und eine vorhandene `rtk.env` bleiben erhalten. Ein `git pull` allein aktualisiert die separat installierte Anwendung nicht. Danach die Browserseite neu laden und die Anzeige **v6.1.1** prüfen.

Bei einem Fehler durch eine ältere `parcel_db.py` muss die ALKIS-Datenbank nicht neu aufgebaut werden. Der Kompatibilitätsweg ersetzt keine Reparatur einer tatsächlich beschädigten SQLite-Datei.

## Landesweite ALKIS-Datenbank

Die große Datenbank liegt **nicht im Repository**. Sie wird aus dem amtlichen Open-Data-Download des LVermGeo Sachsen-Anhalt erzeugt.

### Windows

PowerShell im Repository öffnen:

```powershell
Set-ExecutionPolicy -Scope Process Bypass
.\scripts\STATEWIDE_WINDOWS.ps1
```

Das Skript:

1. lädt `GBIS_Flurstuecke.zip` vom LVermGeo,
2. wandelt die GML/XML-Daten in eine kompakte SQLite-Datenbank um,
3. erzeugt einen R-Tree-Raumindex,
4. kopiert die fertige Datenbank auf `rtk.local`,
5. aktiviert sie auf dem Pi.

Die Datenbank enthält **keine Eigentümerdaten**; das Projekt verarbeitet nur die bereitgestellten Open-Data-Flurstücksinformationen.

### Nur Datenbank bauen

```bash
python tools/build_alkis_db.py GBIS_Flurstuecke.zip alkis_st.sqlite
python tools/db_info.py alkis_st.sqlite
```

Die Geometrien bleiben mit Millimeterauflösung in **ETRS89 / UTM Zone 32N (EPSG:25832)** gespeichert und werden komprimiert abgelegt.

## Offline-Karte

Auf dem Pi:

```bash
chmod +x scripts/download_offline_map.sh
./scripts/download_offline_map.sh
```

Die Karte wird als Geofabrik-Shortbread-MBTiles gespeichert. Die Weboberfläche stellt die Vektorkacheln lokal bereit; unterwegs ist dafür keine Internetverbindung nötig.

## RTK und SAPOS

Die Standardkonfiguration liegt in `rtk.env.example`:

```ini
NTRIP_HOST=4G.sapos-lsa-ntrip.de
NTRIP_PORT=2101
NTRIP_MOUNTPOINT=VRS_3_4G_ST
NTRIP_USER=user
NTRIP_PASSWORD=user
```

Vor dem Einsatz bitte die aktuellen Angaben des LVermGeo prüfen:

- SAPOS HEPS / NTRIP: https://www.lvermgeo.sachsen-anhalt.de/de/gdp-heps-korrekturdatenabgabe.html
- SAPOS FAQ: https://www.lvermgeo.sachsen-anhalt.de/de/sapos_faq.html

Für die Zentimetersuche sollte die Anzeige **RTK FIX** zeigen. `RTK FLOAT`, normales GNSS oder DGNSS sind dafür nicht gleichwertig.

SAPOS empfiehlt bei HEPS Kontroll-/Doppelmessungen. Das Projekt zeigt deshalb den Fixstatus deutlich an, ersetzt aber keine vermessungstechnische Qualitätskontrolle.

## Flurstücke und Zielpunkte

Ein geladenes Flurstück liefert automatisch:

- **P1, P2, …**: Stütz-/Eckpunkte der digitalen ALKIS-Polygongeometrie
- **Z1, Z2, …**: optional erzeugte Hilfspunkte auf geraden Segmenten

Die P-/Z-Bezeichnungen sind **Arbeitsbezeichnungen des Projekts**, keine amtlichen Grenzpunktnummern.

Hilfspunkte können mit 2 m, 5 m oder 10 m Abstand erzeugt werden. Auf der Karte lassen sich Zielpunkte antippen und direkt als Navigationsziel übernehmen.

## Höhenmodus

Version 6 trennt die Höhenmessung in drei einfache Ansichten: **Messen**, **Punkte** und **Einstellungen**. Die Hauptansicht zeigt nur die Informationen, die draußen direkt benötigt werden: RTK-Status, Isthöhe, Sollabweichung, „zu hoch / zu tief“ und die Schaltfläche zum Speichern eines Messpunkts.

### Relative Höhe

Für relative Messungen wird zuerst bewusst ein **Referenzpunkt (REF)** aufgenommen. Danach wird eine Sollhöhe relativ zu diesem Punkt vorgegeben, z. B. `+0.300 m`. Das UI zeigt live die Abweichung in Millimetern und eindeutig **ZU HOCH** bzw. **ZU TIEF**.

> Intern bleibt aus Kompatibilitätsgründen die Bezeichnung `AGL` in der gespeicherten Konfiguration erhalten. Im Benutzerinterface heißt der Modus verständlicher **Relative Höhe**.

### Absolute Höhe (MSL)

Im MSL-Modus wird direkt gegen eine absolute orthometrische Sollhöhe gearbeitet, z. B. `78.450 m`. Grundlage ist die vom GNSS/NMEA-GGA gemeldete Höhe über Geoid/Meeresspiegel, korrigiert um die eingestellte Stabhöhe.

### Höhenpunkte auf der Karte

Gespeicherte Messungen erscheinen direkt in der Offlinekarte:

- **REF**: aktiver Referenzpunkt, blau hervorgehoben
- **H1, H2, H3 …**: normale Höhenmesspunkte
- **grün / gelb / rot**: Status der Sollabweichung
- Klick auf einen Punkt zeigt MSL, relative Höhe, Abweichung, Sigma, Messdauer und Sample-Anzahl
- Punkte können direkt **angesteuert**, **als Referenz gesetzt**, **benannt** oder **gelöscht** werden

Sollabweichung und Messqualität werden getrennt bewertet: Ein Punkt kann also in der Solltoleranz liegen, aber wegen hoher Streuung trotzdem als qualitativ schwach gekennzeichnet sein.

Die Messdauer, Stabhöhe und Toleranz liegen in **Einstellungen**, damit die eigentliche Messansicht übersichtlich bleibt. Bestehende `height_points.json`-Dateien aus v5 werden weiter eingelesen.

> [!WARNING]
> RTK-GNSS ist in der Höhe typischerweise schwächer als in der Lage. Für millimetergenaue Endkontrollen an Bodenplatten, Schalungen oder Fundamenten sollte zusätzlich ein Nivelliergerät bzw. Rotationslaser verwendet werden.

## Datenquellen

### ALKIS / LVermGeo Sachsen-Anhalt

Amtlicher Open-Data-Download:

```text
https://www.geodatenportal.sachsen-anhalt.de/gfds_webshare/download/LVermGeo/Geodatenportal/externedaten/GBIS_Flurstuecke.zip
```

Metadaten / WFS: https://www.geodatenportal.sachsen-anhalt.de/

Die Daten stehen unter den jeweils angegebenen Nutzungsbedingungen, insbesondere der **Datenlizenz Deutschland – Namensnennung 2.0**. Dieses Repository enthält die Daten selbst nicht.

### OpenStreetMap / Geofabrik

Offline-Basiskarte:

```text
https://download.geofabrik.de/europe/germany/sachsen-anhalt-shortbread-1.0.mbtiles
```

OpenStreetMap-Daten stehen unter ODbL. Attribution und Nutzungsbedingungen sind zu beachten.

## Verzeichnisstruktur

```text
.
├── app.py                         # Flask-Webapp, GNSS, NTRIP, Karte, Höhenmodus
├── parcel_db.py                   # lokale ALKIS-Abfragen + R-Tree
├── install.sh                     # Installation auf Raspberry Pi OS
├── rtk-rover.service              # systemd Autostart
├── rtk.env.example                # NTRIP-Konfiguration
├── requirements.txt
├── data/                          # lokale DB/MBTiles (gitignored)
├── static/                        # lokal gecachte MapLibre-Dateien
├── scripts/
│   ├── STATEWIDE_WINDOWS.ps1      # landesweite ALKIS-DB bauen + übertragen
│   ├── STATEWIDE_WINDOWS.cmd
│   └── download_offline_map.sh
├── tools/
│   ├── build_alkis_db.py
│   ├── download_statewide_alkis.py
│   └── db_info.py
└── docs/
    ├── hero.svg
    ├── architecture.svg
    └── ui-preview.svg
```

## Diagnose

```bash
systemctl status rtk-rover
journalctl -u rtk-rover -f
ls -l /dev/serial0
```

Webserver lokal testen:

```bash
curl http://127.0.0.1:5000/api/status
```

## Datenschutz

Die Anwendung benötigt keinen Cloud-Account und sendet die lokalen ALKIS-Daten nicht an einen externen Dienst. Für RTK wird lediglich die aktuelle NMEA-GGA-Näherungsposition an den konfigurierten NTRIP-Caster übermittelt, wie für VRS-Korrekturdaten erforderlich.

## Mitmachen

Fehlerberichte und Pull Requests sind willkommen. Siehe [CONTRIBUTING.md](CONTRIBUTING.md).

## Lizenz

Der **Programmcode** dieses Projekts steht unter der [MIT License](LICENSE).

Für ALKIS-, SAPOS-, OpenStreetMap- und Geofabrik-Daten gelten jeweils deren eigene Nutzungsbedingungen. Siehe [NOTICE.md](NOTICE.md).
