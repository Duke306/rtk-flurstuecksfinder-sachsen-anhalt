#!/bin/bash
set -e
mkdir -p /home/pi/rtk-rover/data
cd /home/pi/rtk-rover/data
URL="https://download.geofabrik.de/europe/germany/sachsen-anhalt-shortbread-1.0.mbtiles"
echo "Lade ca. 260 MB Offline-Karte von Geofabrik..."
curl -L --fail --retry 3 -C - -o sachsen-anhalt-shortbread-1.0.mbtiles "$URL"
echo "Fertig."
sudo systemctl restart rtk-rover
