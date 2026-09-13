#!/bin/bash
set -euo pipefail
if [ "$(id -u)" -eq 0 ]; then echo "Bitte als normaler Benutzer pi ausführen, nicht mit sudo."; exit 1; fi
HERE="$(cd "$(dirname "$0")" && pwd)"
BASE=/home/pi/rtk-rover

echo "[1/5] Systempakete installieren ..."
sudo apt update
sudo apt install -y python3-serial python3-flask python3-pyproj avahi-daemon curl sqlite3
sudo usermod -aG dialout pi

echo "[2/5] Anwendung installieren ..."
mkdir -p "$BASE"/{data,static}
[ -f "$BASE/app.py" ] && cp "$BASE/app.py" "$BASE/app.py.backup.$(date +%Y%m%d_%H%M%S)" || true
cp "$HERE/app.py" "$HERE/parcel_db.py" "$BASE/"
[ -f "$HERE/rtk.env.example" ] && [ ! -f "$BASE/rtk.env" ] && cp "$HERE/rtk.env.example" "$BASE/rtk.env" || true

echo "[3/5] MapLibre für Offline-Betrieb laden ..."
if [ ! -s "$BASE/static/maplibre-gl.js" ]; then
  curl -L --fail --retry 3 -o "$BASE/static/maplibre-gl.js" https://unpkg.com/maplibre-gl@5.24.0/dist/maplibre-gl.js
  curl -L --fail --retry 3 -o "$BASE/static/maplibre-gl.css" https://unpkg.com/maplibre-gl@5.24.0/dist/maplibre-gl.css
fi

echo "[4/5] systemd + mDNS ..."
sudo cp "$HERE/rtk-rover.service" /etc/systemd/system/rtk-rover.service
sudo systemctl daemon-reload
sudo systemctl enable rtk-rover avahi-daemon
sudo systemctl restart avahi-daemon
sudo systemctl restart rtk-rover

echo "[5/5] Fertig"
echo "Weboberfläche: http://rtk.local:5000"
echo "ALKIS-DB:       $BASE/data/alkis_st.sqlite"
echo "Offline-Karte: $BASE/data/sachsen-anhalt-shortbread-1.0.mbtiles"
