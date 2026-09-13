# Daten und Lizenzen

## ALKIS

Die Anwendung lädt oder verarbeitet den amtlichen Open-Data-Flurstücksdownload Sachsen-Anhalts. Die erzeugte SQLite-Datenbank wird nicht in Git eingecheckt.

Quelle:
https://www.geodatenportal.sachsen-anhalt.de/gfds_webshare/download/LVermGeo/Geodatenportal/externedaten/GBIS_Flurstuecke.zip

CRS der gespeicherten Geometrien: **EPSG:25832**.

## Offline-Karte

Quelle:
https://download.geofabrik.de/europe/germany/sachsen-anhalt-shortbread-1.0.mbtiles

Die MBTiles-Datei wird ebenfalls nicht in Git eingecheckt.

## SAPOS

NTRIP-Konfiguration ist bewusst über `rtk.env` überschreibbar. Vor Nutzung immer die offiziellen aktuellen Daten prüfen.
