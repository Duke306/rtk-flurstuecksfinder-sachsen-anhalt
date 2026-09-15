# Changelog

## Unreleased

- Geometrie-Cache mit unveränderlichen Werten, maximal 128 Einträgen und 8 MiB bilanziertem Speicherbudget (#3)
- Gebündelte Koordinatenumrechnung für Geometrien und Navigationsziele (#4)
- Kartenstil ohne JSON-Kopierrundlauf; Vorlage bleibt unverändert (#6)
- Flurstückszuordnung prüft weiterhin jede aktuelle Position ohne Bewegungsschwelle

## 0.6.0 — 2026-09

- Integration erhält den Ein-Abfrage-Fix aus PR #7
- V5-Bezeichnungen, Stabhöhe 0 m und individuelle Messdauer/Toleranz bleiben erhalten
- Gleichzeitige Messversuche werden abgefangen; Einstellungen während einer Messung bleiben erhalten
- Kartenfokus wartet auf das Laden der Karte

- Höhenmodus komplett neu strukturiert: **Messen / Punkte / Einstellungen**
- bewusstes Aufnehmen eines Referenzpunkts statt automatischer erster Referenz
- automatische Höhenpunktnamen `H1`, `H2`, `H3` …
- gespeicherte Höhenpunkte und `REF` direkt auf der Offlinekarte
- Karten-Popups mit MSL, relativer Höhe, Sollabweichung, Sigma, Dauer und Samples
- Höhenpunkte können angesteuert, umbenannt, als Referenz gesetzt und gelöscht werden
- große Live-Anzeige „ZU HOCH / ZU TIEF“
- Sollabweichung und Messqualität getrennt dargestellt
- Fortschrittsanzeige während der Höhenmessung
- Kartenfilter für Flurstück, Grenzpunkte und Höhenpunkte
- Stabhöhe, Messdauer und Toleranz in die Einstellungen verschoben
- kompatibles Einlesen vorhandener `height_points.json` aus v5
- README-Clone-URL auf das echte Repository korrigiert

## 0.5.0 — 2026-09

- Landesweite ALKIS-Datenbank für Sachsen-Anhalt
- Offline-Karte via Shortbread-MBTiles / MapLibre
- Automatische Flurstückserkennung an der RTK-Position
- Auswahl Landkreis → Gemarkung → Flur → Flurstück
- ALKIS-Eckpunkte `P1…` und optionale Zwischenpunkte `Z1…`
- Grenzpunktnavigation mit Abstand und Richtung
- Höhenmodus mit RTK-FIX-Gate, Mittelwert, Sigma und Spannweite
- Sollhöhen relativ (AGL) und absolut (MSL) mit Toleranz/Ampel
- WLAN-, Internet-, SAPOS- und RTK-Status
