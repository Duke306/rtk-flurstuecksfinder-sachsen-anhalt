from flask import Flask, render_template_string, jsonify, request, Response
from pyproj import Transformer
from parcel_db import ParcelDB
import base64, math, os, socket, sqlite3, subprocess, threading, time, serial, json, statistics
from collections import deque
from functools import wraps

BASE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(BASE, 'data')
STATIC = os.path.join(BASE, 'static')
PARCEL_DB = os.environ.get('RTK_PARCEL_DB', os.path.join(DATA, 'alkis_st.sqlite'))
MAP_DB = os.environ.get('RTK_MAP_DB', os.path.join(DATA, 'sachsen-anhalt-shortbread-1.0.mbtiles'))
HEIGHT_FILE = os.environ.get('RTK_HEIGHT_FILE', os.path.join(DATA, 'height_points.json'))

app = Flask(__name__, static_folder=STATIC)
parcels = ParcelDB(PARCEL_DB) if os.path.exists(PARCEL_DB) else None

SERIAL_BAUD = 115200
SERIAL_CANDIDATES = ['/dev/serial0', '/dev/ttyS0', '/dev/ttyAMA0', '/dev/ttyUSB0']
NTRIP_HOST = os.environ.get('NTRIP_HOST', '4G.sapos-lsa-ntrip.de')
NTRIP_PORT = int(os.environ.get('NTRIP_PORT', '2101'))
NTRIP_MOUNTPOINT = os.environ.get('NTRIP_MOUNTPOINT', 'VRS_3_4G_ST')
NTRIP_USER = os.environ.get('NTRIP_USER', 'user')
NTRIP_PASSWORD = os.environ.get('NTRIP_PASSWORD', 'user')
SIM_START_E = 671730.0
SIM_START_N = 5738312.0

state_lock = threading.Lock()
serial_write_lock = threading.Lock()
height_file_lock = threading.Lock()
height_update_lock = threading.RLock()
height_capture_lock = threading.Lock()
height_samples = deque(maxlen=1200)
state = {
    'serial_connected': False, 'serial_port': None, 'serial_error': '', 'serial_obj': None,
    'last_nmea': '', 'last_gga': '', 'last_fix_time': 0.0, 'lat': None, 'lon': None,
    'alt': None, 'geoid_sep': None, 'E': SIM_START_E, 'N': SIM_START_N, 'quality': 0,
    'fix': 'SIMULATION', 'sats': 0, 'hdop': None, 'ntrip': 'WARTET AUF LC29H',
    'ntrip_error': '', 'rtcm_bytes': 0, 'wifi_ssid': '', 'local_ip': '', 'internet': False,
}
geo_to_utm = Transformer.from_crs('EPSG:4258', 'EPSG:25832', always_xy=True)
utm_to_geo = Transformer.from_crs('EPSG:25832', 'EPSG:4326', always_xy=True)

# ---------- GNSS / SAPOS ----------
def nmea_degrees(v, h):
    if not v:
        return None
    raw = float(v)
    deg = int(raw // 100)
    m = raw - deg * 100
    r = deg + m / 60
    return -r if h in ('S', 'W') else r


def fix_name(q):
    return {0: 'NO FIX', 1: 'GNSS', 2: 'DGPS', 3: 'PPS', 4: 'RTK FIX', 5: 'RTK FLOAT', 6: 'ESTIMATED'}.get(q, f'FIX {q}')


def parse_gga(line):
    f = line.split('*', 1)[0].split(',')
    if len(f) < 10 or not f[0].endswith('GGA'):
        return False
    try:
        lat = nmea_degrees(f[2], f[3])
        lon = nmea_degrees(f[4], f[5])
        q = int(f[6] or 0)
        sats = int(f[7] or 0)
        hdop = float(f[8]) if f[8] else None
        alt = float(f[9]) if f[9] else None
        geoid_sep = float(f[11]) if len(f) > 11 and f[11] else None
    except (ValueError, IndexError):
        return False
    if lat is None or lon is None:
        return False
    E, N = geo_to_utm.transform(lon, lat)
    now = time.time()
    sample = {'t': now, 'quality': q, 'fix': fix_name(q), 'alt': alt, 'E': E, 'N': N,
              'hdop': hdop, 'sats': sats, 'lat': lat, 'lon': lon}
    with state_lock:
        state.update(serial_connected=True, serial_error='', last_gga=line.strip(), last_nmea=line.strip(),
                     last_fix_time=now, lat=lat, lon=lon, alt=alt, geoid_sep=geoid_sep, quality=q,
                     fix=fix_name(q), sats=sats, hdop=hdop, E=E, N=N)
        if alt is not None:
            height_samples.append(sample)
    return True


def choose_serial_port():
    return next((p for p in SERIAL_CANDIDATES if os.path.exists(p)), None)


def serial_worker():
    while True:
        port = choose_serial_port()
        if not port:
            with state_lock:
                state.update(serial_connected=False, serial_port=None, serial_obj=None,
                             fix='SIMULATION', serial_error='Kein UART-Gerät gefunden')
            time.sleep(2)
            continue
        ser = None
        try:
            ser = serial.Serial(port, SERIAL_BAUD, timeout=1.0, write_timeout=2.0)
            ser.reset_input_buffer()
            with state_lock:
                state.update(serial_port=port, serial_obj=ser, serial_error='UART offen, warte auf NMEA')
            last = 0
            while True:
                raw = ser.readline()
                if not raw:
                    if last and time.time() - last > 4:
                        with state_lock:
                            state.update(serial_connected=False, fix='NO DATA')
                    continue
                line = raw.decode('ascii', errors='ignore').strip()
                if not line.startswith('$'):
                    continue
                last = time.time()
                with state_lock:
                    state.update(last_nmea=line, serial_error='')
                if line.split('*', 1)[0].split(',', 1)[0].endswith('GGA'):
                    parse_gga(line)
        except Exception as e:
            with state_lock:
                state.update(serial_connected=False, serial_obj=None, serial_error=str(e), fix='SIMULATION')
            try:
                if ser:
                    ser.close()
            except Exception:
                pass
            time.sleep(2)


def get_wifi_ssid():
    try:
        r = subprocess.run(['nmcli', '-t', '-f', 'ACTIVE,SSID', 'dev', 'wifi'], capture_output=True, text=True, timeout=2)
        for line in r.stdout.splitlines():
            if line.startswith('yes:'):
                return line.split(':', 1)[1] or '(verbunden)'
    except Exception:
        pass
    return ''


def get_local_ip():
    try:
        ips = subprocess.run(['hostname', '-I'], capture_output=True, text=True, timeout=2).stdout.strip().split()
        return ips[0] if ips else ''
    except Exception:
        return ''


def internet_ok():
    try:
        s = socket.create_connection(('1.1.1.1', 443), timeout=1.5)
        s.close()
        return True
    except Exception:
        return False


def network_worker():
    while True:
        with state_lock:
            state.update(wifi_ssid=get_wifi_ssid(), local_ip=get_local_ip(), internet=internet_ok())
        time.sleep(3)


def ntrip_request():
    auth = base64.b64encode(f'{NTRIP_USER}:{NTRIP_PASSWORD}'.encode()).decode()
    return (f'GET /{NTRIP_MOUNTPOINT} HTTP/1.1\r\n'
            f'Host: {NTRIP_HOST}:{NTRIP_PORT}\r\n'
            'Ntrip-Version: Ntrip/2.0\r\n'
            'User-Agent: NTRIP RTK-Rover-ST/2.0\r\n'
            f'Authorization: Basic {auth}\r\n'
            'Connection: close\r\n\r\n').encode()


def get_gga_bytes():
    with state_lock:
        g = state.get('last_gga', '')
    return (g + '\r\n').encode() if g else None


def write_rtcm(data):
    if not data:
        return
    with state_lock:
        ser = state.get('serial_obj')
    if not ser:
        return
    try:
        with serial_write_lock:
            ser.write(data)
        with state_lock:
            state['rtcm_bytes'] += len(data)
    except Exception as e:
        with state_lock:
            state['ntrip_error'] = f'RTCM -> GNSS: {e}'


def ntrip_worker():
    while True:
        with state_lock:
            connected = state['serial_connected']
            gga = bool(state['last_gga'])
        if not connected:
            with state_lock:
                state['ntrip'] = 'WARTET AUF LC29H'
            time.sleep(2)
            continue
        if not gga:
            with state_lock:
                state['ntrip'] = 'WARTET AUF POSITION'
            time.sleep(1)
            continue
        sock = None
        try:
            with state_lock:
                state.update(ntrip='VERBINDET', ntrip_error='')
            sock = socket.create_connection((NTRIP_HOST, NTRIP_PORT), timeout=10)
            sock.sendall(ntrip_request())
            resp = b''
            while b'\r\n\r\n' not in resp and len(resp) < 16384:
                c = sock.recv(4096)
                if not c:
                    raise ConnectionError('Caster hat Verbindung beendet')
                resp += c
            head, _, rest = resp.partition(b'\r\n\r\n')
            first = head.split(b'\r\n', 1)[0].decode(errors='ignore')
            if '200' not in first and not first.startswith('ICY 200'):
                raise ConnectionError(first)
            if rest:
                write_rtcm(rest)
            g = get_gga_bytes()
            if g:
                sock.sendall(g)
            sock.settimeout(1)
            last = time.time()
            with state_lock:
                state['ntrip'] = 'RTCM STREAM'
            while True:
                with state_lock:
                    if not state['serial_connected']:
                        raise ConnectionError('GNSS-Daten abgebrochen')
                if time.time() - last >= 5:
                    g = get_gga_bytes()
                    if g:
                        sock.sendall(g)
                    last = time.time()
                try:
                    d = sock.recv(4096)
                    if not d:
                        raise ConnectionError('RTCM-Stream beendet')
                    write_rtcm(d)
                except socket.timeout:
                    pass
        except Exception as e:
            with state_lock:
                state.update(ntrip='GETRENNT', ntrip_error=str(e))
            time.sleep(3)
        finally:
            try:
                if sock:
                    sock.close()
            except Exception:
                pass


def snap():
    with state_lock:
        return {k: v for k, v in state.items() if k != 'serial_obj'}


def arrow(b):
    return ['↑', '↗', '→', '↘', '↓', '↙', '←', '↖'][round(b / 45) % 8]

# ---------- Offline MBTiles ----------
map_local = threading.local()

def map_conn():
    c = getattr(map_local, 'c', None)
    if c is None and os.path.exists(MAP_DB):
        c = sqlite3.connect(f'file:{MAP_DB}?mode=ro', uri=True, check_same_thread=False)
        map_local.c = c
    return c


@app.route('/tiles/<int:z>/<int:x>/<int:y>.pbf')
def tile(z, x, y):
    c = map_conn()
    if not c:
        return Response(status=404)
    tms = (1 << z) - 1 - y
    r = c.execute('SELECT tile_data FROM tiles WHERE zoom_level=? AND tile_column=? AND tile_row=?', (z, x, tms)).fetchone()
    if not r:
        return Response(status=204)
    data = r[0]
    resp = Response(data, mimetype='application/x-protobuf')
    resp.headers['Cache-Control'] = 'public,max-age=86400'
    if data[:2] == b'\x1f\x8b':
        resp.headers['Content-Encoding'] = 'gzip'
    return resp


MAP_STYLE = {
    'version': 8,
    'sources': {'osm': {'type': 'vector', 'tiles': ['/tiles/{z}/{x}/{y}.pbf'], 'minzoom': 0, 'maxzoom': 14,
                        'attribution': '© OpenStreetMap contributors'}},
    'layers': [
        {'id': 'bg', 'type': 'background', 'paint': {'background-color': '#eef1e6'}},
        {'id': 'land', 'type': 'fill', 'source': 'osm', 'source-layer': 'land', 'minzoom': 7,
         'paint': {'fill-color': ['match', ['get', 'kind'], 'forest', '#d6e7c5', 'wood', '#d6e7c5',
                                  'farmland', '#ece5c7', 'meadow', '#e3eccf', 'grass', '#e3eccf',
                                  'residential', '#e6e1d8', 'industrial', '#ddd7d0', '#e2e5d4'],
                   'fill-opacity': 0.85}},
        {'id': 'sites', 'type': 'fill', 'source': 'osm', 'source-layer': 'sites', 'minzoom': 12,
         'paint': {'fill-color': '#ddd7c8', 'fill-opacity': 0.65}},
        {'id': 'water', 'type': 'fill', 'source': 'osm', 'source-layer': 'water_polygons', 'minzoom': 4,
         'paint': {'fill-color': '#a9cfe8'}},
        {'id': 'waterline', 'type': 'line', 'source': 'osm', 'source-layer': 'water_lines', 'minzoom': 9,
         'paint': {'line-color': '#83b9dd', 'line-width': 1.3}},
        {'id': 'boundaries', 'type': 'line', 'source': 'osm', 'source-layer': 'boundaries',
         'paint': {'line-color': '#999', 'line-width': 1, 'line-dasharray': [3, 2]}},
        {'id': 'roads-low', 'type': 'line', 'source': 'osm', 'source-layer': 'streets_low', 'minzoom': 5, 'maxzoom': 11,
         'paint': {'line-color': '#c0aa86', 'line-width': ['interpolate', ['linear'], ['zoom'], 5, 0.4, 10, 2.2]}},
        {'id': 'roads-med', 'type': 'line', 'source': 'osm', 'source-layer': 'streets_med', 'minzoom': 8, 'maxzoom': 14,
         'paint': {'line-color': '#bbb09e', 'line-width': ['interpolate', ['linear'], ['zoom'], 8, 0.6, 14, 3.5]}},
        {'id': 'streets-outline', 'type': 'line', 'source': 'osm', 'source-layer': 'streets', 'minzoom': 10,
         'paint': {'line-color': '#aaa', 'line-width': ['interpolate', ['linear'], ['zoom'], 10, 1.4, 14, 4.8]}},
        {'id': 'streets', 'type': 'line', 'source': 'osm', 'source-layer': 'streets', 'minzoom': 10,
         'paint': {'line-color': '#fff', 'line-width': ['interpolate', ['linear'], ['zoom'], 10, 0.8, 14, 3.5]}},
        {'id': 'buildings', 'type': 'fill', 'source': 'osm', 'source-layer': 'buildings', 'minzoom': 14,
         'paint': {'fill-color': '#cdbdaf', 'fill-outline-color': '#9f9185'}},
    ],
}


@app.route('/api/map/style')
def map_style():
    style = json.loads(json.dumps(MAP_STYLE))
    style['sources']['osm']['tiles'] = [request.url_root.rstrip('/') + '/tiles/{z}/{x}/{y}.pbf']
    return jsonify(style)


@app.route('/api/map/info')
def map_info():
    info = {'exists': os.path.exists(MAP_DB), 'path': MAP_DB, 'size_mb': 0, 'tiles': 0,
            'minzoom': None, 'maxzoom': None, 'error': ''}
    if not info['exists']:
        return jsonify(info)
    try:
        info['size_mb'] = round(os.path.getsize(MAP_DB) / 1024 / 1024, 1)
        c = sqlite3.connect(f'file:{MAP_DB}?mode=ro', uri=True)
        try:
            r = c.execute('SELECT COUNT(*),MIN(zoom_level),MAX(zoom_level) FROM tiles').fetchone()
            if r:
                info['tiles'], info['minzoom'], info['maxzoom'] = int(r[0]), r[1], r[2]
        finally:
            c.close()
    except Exception as e:
        info['error'] = str(e)
    return jsonify(info)

# ---------- Parcel APIs ----------
def db_required():
    return parcels is not None and os.path.exists(PARCEL_DB)


@app.route('/api/dbinfo')
def dbinfo():
    return jsonify(alkis=db_required(), map=os.path.exists(MAP_DB), alkis_path=PARCEL_DB, map_path=MAP_DB)


@app.route('/api/counties')
def counties():
    return jsonify(parcels.counties() if db_required() else [])


@app.route('/api/gemarkungen')
def gemarkungen():
    return jsonify(parcels.gemarkungen(request.args.get('kreis', '')) if db_required() else [])


@app.route('/api/fluren')
def fluren():
    return jsonify(parcels.fluren(request.args.get('gemaschl', '')) if db_required() else [])


@app.route('/api/flurstuecke')
def flurstuecke():
    if not db_required():
        return jsonify([])
    try:
        return jsonify(parcels.flurstuecke(request.args['gemaschl'], request.args['flur']))
    except Exception:
        return jsonify([])


@app.route('/api/parcel/<int:pid>')
def parcel(pid):
    if not db_required():
        return jsonify(error='Keine ALKIS-Datenbank'), 404
    p, gj = parcels.parcel_and_geojson(pid)
    if not p:
        return jsonify(error='nicht gefunden'), 404
    return jsonify(parcel=p, feature=gj)


@app.route('/api/parcel/<int:pid>/targets')
def parcel_targets(pid):
    if not db_required():
        return jsonify([])
    try:
        spacing = float(request.args.get('spacing', '0'))
    except Exception:
        spacing = 0
    ts = parcels.targets(pid, spacing)
    for t in ts:
        t['lon'], t['lat'] = utm_to_geo.transform(t['E'], t['N'])
    return jsonify(ts)


@app.route('/api/parcels/near')
def parcels_near():
    if not db_required():
        return jsonify([])
    try:
        E = float(request.args['E'])
        N = float(request.args['N'])
        r = min(float(request.args.get('r', 120)), 500)
    except Exception:
        return jsonify([])
    return jsonify(parcels.near(E, N, r))

# ---------- Rover / navigation ----------
@app.route('/api/status')
def status():
    s = snap()
    sim = not s['serial_connected']
    E, N = s['E'], s['N']
    targetE, targetN = request.args.get('E'), request.args.get('N')
    nav = None
    if targetE is not None and targetN is not None:
        try:
            te, tn = float(targetE), float(targetN)
            de, dn = te - E, tn - N
            d = math.hypot(de, dn)
            b = math.degrees(math.atan2(de, dn)) % 360
            nav = {'distance': d, 'dE': de, 'dN': dn, 'bearing': b, 'arrow': arrow(b),
                   'target_E': te, 'target_N': tn}
        except Exception:
            pass
    current = []
    if db_required() and s['lat'] is not None:
        try:
            current = parcels.at_point(E, N)
        except Exception:
            current = []
    age = time.time() - s['last_fix_time'] if s['last_fix_time'] else None
    return jsonify(simulation=sim, fix='SIMULATION' if sim else s['fix'], E=E, N=N,
                   lat=s['lat'], lon=s['lon'], alt=s['alt'], geoid_sep=s.get('geoid_sep'),
                   sats=s['sats'], hdop=s['hdop'], age=age, serial_port=s['serial_port'],
                   serial_error=s['serial_error'], wifi_ssid=s['wifi_ssid'], local_ip=s['local_ip'],
                   internet=s['internet'], ntrip=s['ntrip'], ntrip_error=s['ntrip_error'],
                   rtcm_bytes=s['rtcm_bytes'], current_parcels=current, nav=nav)


@app.route('/api/nearest-boundary/<int:pid>')
def nearest_boundary(pid):
    if not db_required():
        return jsonify(error='Keine ALKIS-DB'), 404
    s = snap()
    p = parcels.get(pid, True)
    if not p:
        return jsonify(error='nicht gefunden'), 404
    d, pt = parcels.nearest_boundary_point(s['E'], s['N'], p['polys'])
    lon, lat = utm_to_geo.transform(*pt)
    return jsonify(distance=d, E=pt[0], N=pt[1], lon=lon, lat=lat)

# ---------- Höhenmessung v6 ----------
def _height_defaults():
    return {
        'reference_id': None,
        'points': [],
        'settings': {
            'mode': 'AGL',               # intern kompatibel; UI nennt es "Relative Höhe"
            'target_msl_m': None,
            'target_agl_m': 0.0,
            'tolerance_mm': 20.0,
            'pole_height_m': 2.0,
            'duration_s': 10.0,
            'quality_good_mm': 10.0,
            'quality_warn_mm': 20.0,
        },
    }


def _height_store_load():
    with height_file_lock:
        try:
            with open(HEIGHT_FILE, 'r', encoding='utf-8') as f:
                d = json.load(f)
            if not isinstance(d, dict):
                raise ValueError()
        except Exception:
            d = _height_defaults()
    d.setdefault('reference_id', None)
    d.setdefault('points', [])
    d.setdefault('settings', {})
    st = d['settings']
    defaults = _height_defaults()['settings']
    for k, v in defaults.items():
        st.setdefault(k, v)
    if st.get('mode') not in ('AGL', 'MSL'):
        st['mode'] = 'AGL'
    # Rückwärtskompatibilität zu v5: fehlende Felder nur ergänzen, alte Messungen nicht verändern.
    for p in d['points']:
        pid = int(p.get('id', 0) or 0)
        p.setdefault('role', 'point')
        p.setdefault('code', f'H{pid}' if pid else 'H?')
        p.setdefault('description', p.get('name', ''))
    return d


def _height_store_save(d):
    os.makedirs(os.path.dirname(os.path.abspath(HEIGHT_FILE)), exist_ok=True)
    tmp = HEIGHT_FILE + '.tmp'
    with height_file_lock:
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(d, f, ensure_ascii=False, indent=2)
        os.replace(tmp, HEIGHT_FILE)


def _height_level(delta_m, tolerance_mm):
    if delta_m is None:
        return None
    tol = max(0.001, float(tolerance_mm)) / 1000.0
    a = abs(delta_m)
    if a <= tol:
        return 'green'
    if a <= 2 * tol:
        return 'yellow'
    return 'red'


def _height_quality(sigma_m, st):
    if sigma_m is None:
        return None
    mm = float(sigma_m) * 1000.0
    if mm <= float(st.get('quality_good_mm', 10)):
        return 'green'
    if mm <= float(st.get('quality_warn_mm', 20)):
        return 'yellow'
    return 'red'


def _height_result():
    d = _height_store_load()
    ref = next((p for p in d['points'] if str(p.get('id')) == str(d.get('reference_id'))), None)
    st = d['settings']
    tol = st.get('tolerance_mm', 20.0)
    target_msl = st.get('target_msl_m')
    target_agl = st.get('target_agl_m')
    for p in d['points']:
        p['is_reference'] = bool(ref and str(p.get('id')) == str(ref.get('id')))
        p['display_code'] = 'REF' if p['is_reference'] else p.get('code', f"H{p.get('id','?')}")
        p['agl_m'] = (p['height_m'] - ref['height_m']) if ref else None
        p['delta_reference_m'] = p['agl_m']
        p['delta_msl_m'] = (p['height_m'] - float(target_msl)) if target_msl is not None else None
        p['delta_agl_m'] = (p['agl_m'] - float(target_agl)) if p['agl_m'] is not None and target_agl is not None else None
        active = p['delta_msl_m'] if st.get('mode') == 'MSL' else p['delta_agl_m']
        p['active_delta_m'] = active
        p['level'] = _height_level(active, tol)
        p['quality_level'] = _height_quality(p.get('sigma_m'), st)
        try:
            p['lon'], p['lat'] = utm_to_geo.transform(float(p['E']), float(p['N']))
        except Exception:
            p['lon'], p['lat'] = None, None
    d['reference_height_m'] = ref['height_m'] if ref else None
    d['reference'] = ref
    return d


def height_update(fn):
    @wraps(fn)
    def wrapped(*args, **kwargs):
        with height_update_lock:
            return fn(*args, **kwargs)
    return wrapped


def single_height_capture(fn):
    @wraps(fn)
    def wrapped(*args, **kwargs):
        if not height_capture_lock.acquire(blocking=False):
            return jsonify(error='Eine Höhenmessung läuft bereits.'), 409
        try:
            return fn(*args, **kwargs)
        finally:
            height_capture_lock.release()
    return wrapped


@app.route('/api/height/points')
def height_points():
    return jsonify(_height_result())


@app.route('/api/height/settings', methods=['GET', 'POST'])
@height_update
def height_settings():
    d = _height_store_load()
    if request.method == 'POST':
        cfg = request.get_json(silent=True) or {}
        st = d['settings']
        mode = str(cfg.get('mode', st.get('mode', 'AGL'))).upper()
        if mode in ('AGL', 'MSL'):
            st['mode'] = mode

        def optional_float(v, old):
            if v in (None, ''):
                return None
            try:
                return float(v)
            except Exception:
                return old

        if 'target_msl_m' in cfg:
            st['target_msl_m'] = optional_float(cfg.get('target_msl_m'), st.get('target_msl_m'))
        if 'target_agl_m' in cfg:
            st['target_agl_m'] = optional_float(cfg.get('target_agl_m'), st.get('target_agl_m'))
        if st.get('target_agl_m') is None:
            st['target_agl_m'] = 0.0
        for key, lo, hi in (
            ('tolerance_mm', 1, 200), ('pole_height_m', 0, 10), ('duration_s', 3, 60),
            ('quality_good_mm', 1, 100), ('quality_warn_mm', 1, 200),
        ):
            if key in cfg:
                try:
                    st[key] = max(lo, min(hi, float(cfg[key])))
                except Exception:
                    pass
        if st['quality_warn_mm'] < st['quality_good_mm']:
            st['quality_warn_mm'] = st['quality_good_mm']
        _height_store_save(d)
    return jsonify(_height_result())


def _next_height_code(points):
    used = set()
    for p in points:
        c = str(p.get('code', ''))
        if c.startswith('H') and c[1:].isdigit():
            used.add(int(c[1:]))
    n = 1
    while n in used:
        n += 1
    return f'H{n}'


@app.route('/api/height/capture', methods=['POST'])
@single_height_capture
def height_capture():
    cfg = request.get_json(silent=True) or {}
    d = _height_store_load()
    st = d['settings']
    role = str(cfg.get('role', 'point')).lower()
    if role not in ('point', 'reference'):
        role = 'point'
    try:
        duration = max(3.0, min(60.0, float(cfg.get('duration', st.get('duration_s', 10)))))
    except Exception:
        duration = 10.0
    try:
        pole = max(0.0, min(10.0, float(cfg.get('pole_height', st.get('pole_height_m', 2.0)))))
    except Exception:
        pole = 2.0
    description = str(cfg.get('description') or '').strip()[:80]

    s = snap()
    if (s.get('fix') != 'RTK FIX' or s.get('alt') is None
            or not s.get('serial_connected') or time.time() - s.get('last_fix_time', 0) > 4):
        return jsonify(error='Höhenmessung nur bei RTK FIX möglich.'), 409

    start = time.time()
    while time.time() < start + duration:
        time.sleep(0.15)

    with state_lock:
        samples = [dict(x) for x in height_samples if x['t'] >= start and x['quality'] == 4 and x['alt'] is not None]
    if len(samples) < max(3, int(duration * 0.5)):
        return jsonify(error=f'Zu wenige gültige RTK-FIX-Höhenwerte ({len(samples)}). Bitte erneut messen.'), 409

    raw_h = [x['alt'] - pole for x in samples]
    med = statistics.median(raw_h)
    good = [(x, h) for x, h in zip(samples, raw_h) if abs(h - med) <= 0.08]
    if len(good) < 3:
        good = list(zip(samples, raw_h))
    hs = [h for _, h in good]
    es = [x['E'] for x, _ in good]
    ns = [x['N'] for x, _ in good]
    mean_h = statistics.fmean(hs)
    sigma = statistics.stdev(hs) if len(hs) > 1 else 0.0
    spread = max(hs) - min(hs) if hs else 0.0

    with height_update_lock:
        d = _height_store_load()  # Preserve edits made while samples were collected.
        new_id = max([int(p.get('id', 0)) for p in d['points']] or [0]) + 1
        code = f'R{new_id}' if role == 'reference' else _next_height_code(d['points'])
        name = 'Referenz' if role == 'reference' else code
        p = {
            'id': new_id, 'code': code, 'role': role, 'name': name, 'description': description,
            'height_m': mean_h, 'antenna_alt_m': mean_h + pole, 'pole_height_m': pole,
            'E': statistics.fmean(es), 'N': statistics.fmean(ns), 'samples': len(hs),
            'sigma_m': sigma, 'spread_m': spread, 'duration_s': duration,
            'created': time.strftime('%Y-%m-%d %H:%M:%S'), 'fix': 'RTK FIX',
        }
        d['points'].append(p)
        if role == 'reference':
            d['reference_id'] = new_id
        _height_store_save(d)
    result = _height_result()
    saved = next((x for x in result['points'] if x['id'] == new_id), p)
    return jsonify(ok=True, point=saved, data=result)


@app.route('/api/height/reference/<int:pid>', methods=['POST'])
@height_update
def height_reference(pid):
    d = _height_store_load()
    if not any(int(p.get('id', -1)) == pid for p in d['points']):
        return jsonify(error='Punkt nicht gefunden'), 404
    d['reference_id'] = pid
    _height_store_save(d)
    return jsonify(ok=True, data=_height_result())


@app.route('/api/height/point/<int:pid>', methods=['PATCH', 'DELETE'])
@height_update
def height_point(pid):
    d = _height_store_load()
    p = next((p for p in d['points'] if int(p.get('id', -1)) == pid), None)
    if not p:
        return jsonify(error='Punkt nicht gefunden'), 404
    if request.method == 'DELETE':
        d['points'] = [x for x in d['points'] if int(x.get('id', -1)) != pid]
        if str(d.get('reference_id')) == str(pid):
            d['reference_id'] = None
        _height_store_save(d)
        return jsonify(ok=True, data=_height_result())
    cfg = request.get_json(silent=True) or {}
    if 'description' in cfg:
        p['description'] = str(cfg.get('description') or '').strip()[:80]
    if 'name' in cfg and str(cfg.get('name') or '').strip():
        p['name'] = str(cfg['name']).strip()[:60]
    _height_store_save(d)
    return jsonify(ok=True, data=_height_result())


@app.route('/api/height/clear', methods=['POST'])
@height_update
def height_clear():
    d = _height_store_load()
    d['reference_id'] = None
    d['points'] = []
    _height_store_save(d)
    return jsonify(ok=True)


HTML = r'''<!doctype html><html lang="de"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1,user-scalable=no">
<title>RTK Flurstücksfinder Sachsen-Anhalt</title><link rel="stylesheet" href="/static/maplibre-gl.css">
<style>
*{box-sizing:border-box}body{margin:0;font-family:Arial,sans-serif;background:#111;color:#eee}.top{padding:10px 12px;background:#181818;position:sticky;top:0;z-index:20}.title{font-size:20px;font-weight:bold}.checks{display:grid;grid-template-columns:repeat(4,1fr);gap:4px;margin-top:8px}.check{background:#272727;border-radius:6px;padding:6px;font-size:11px;text-align:center}.ok,.heightgreen{color:#71e28b}.bad,.heightred{color:#ff7777}.warn,.heightyellow{color:#ffd166}.heightneutral{color:#bbb}.tabs{display:flex;position:sticky;top:77px;background:#111;z-index:19}.tabs button{flex:1;padding:11px;background:#222;color:white;border:0;border-bottom:2px solid #444}.tabs button.active{background:#333;border-bottom-color:#6bc5ff}.pane{display:none;padding:12px}.pane.active{display:block}.card{background:#1d1d1d;border-radius:9px;padding:12px;margin-bottom:10px}.row{display:grid;grid-template-columns:1fr 1fr;gap:8px}select,button,input{font-size:16px;padding:10px;border-radius:7px;border:1px solid #555;background:#292929;color:#fff;width:100%}button.primary{background:#176b2c}button.secondary{background:#285b8f}.big{font-size:54px;font-weight:bold;text-align:center}.arrow{font-size:80px;text-align:center;line-height:90px}.center{text-align:center}.muted{color:#aaa;font-size:12px}.targetlist{max-height:42vh;overflow:auto}.target{display:flex;gap:7px;align-items:center;padding:6px 0;border-bottom:1px solid #333}.target button{width:auto;flex:1;text-align:left}.target .go{max-width:75px;text-align:center;background:#285b8f}.current{border:1px solid #176b2c}.mapwrap{height:calc(100vh - 210px);min-height:430px}.map{height:100%;border-radius:8px}.info{line-height:1.55}.danger{color:#ff9c9c}.smallbtn{padding:7px;font-size:13px}.pointlabel{font:700 12px Arial;background:#101010dd;color:#fff;border:2px solid #f6a000;border-radius:12px;padding:2px 5px;white-space:nowrap;cursor:pointer;box-shadow:0 1px 3px #0008}.pointlabel.helper{border-color:#43a5ff;font-size:11px}.heightnav{display:grid;grid-template-columns:1fr 1fr 1fr;gap:5px;margin-bottom:10px}.heightnav button{padding:9px}.heightnav button.active{background:#285b8f}.hsub{display:none}.hsub.active{display:block}.heightlive{font-size:42px;font-weight:700;text-align:center;margin:8px 0}.heightdelta{font-size:34px;font-weight:800;text-align:center}.traffic{font-size:22px;font-weight:bold;text-align:center;padding:12px;border-radius:8px;background:#292929;margin-top:8px}.direction{font-size:18px;text-align:center;margin:7px 0}.metricgrid{display:grid;grid-template-columns:1fr 1fr;gap:7px}.metric{background:#292929;border-radius:8px;padding:9px;text-align:center}.metric b{font-size:18px}.measurebtn{font-size:20px;padding:15px;margin-top:10px}.progress{height:12px;background:#333;border-radius:10px;overflow:hidden;margin:10px 0}.progress>div{height:100%;width:0;background:#71e28b;transition:width .15s}.resultbox{border:1px solid #3d6;border-radius:9px;padding:10px;margin-top:10px}.heightrow{display:grid;grid-template-columns:1fr auto;gap:8px;padding:10px 0;border-bottom:1px solid #333}.heightrow.ref{border-left:4px solid #43a5ff;padding-left:8px}.badge{display:inline-block;padding:3px 7px;border-radius:10px;background:#333;font-size:11px}.mapfilters{display:flex;gap:6px;flex-wrap:wrap}.mapfilters label{background:#222;border-radius:12px;padding:6px 9px;font-size:12px}.mapfilters input{width:auto;padding:0}.hmarker{font:700 12px Arial;background:#111;color:#fff;border:3px solid #888;border-radius:50%;min-width:32px;height:32px;display:flex;align-items:center;justify-content:center;box-shadow:0 1px 5px #000}.hmarker.green{border-color:#46c565}.hmarker.yellow{border-color:#ffd166}.hmarker.red{border-color:#ff6262}.hmarker.ref{border-color:#43a5ff;border-radius:8px;min-width:42px}.settingnote{font-size:12px;color:#aaa;margin-top:5px}.maplibregl-popup-content{background:#1d1d1d;color:#eee}.maplibregl-popup-close-button{width:auto}.hidden{display:none!important}
</style></head><body>
<div class="top"><div class="title">RTK Flurstücksfinder · Sachsen-Anhalt</div><div class="checks"><div class="check">WLAN<br><b id="wifi">--</b></div><div class="check">Internet<br><b id="inet">--</b></div><div class="check">SAPOS<br><b id="sapos">--</b></div><div class="check">RTK<br><b id="rtk">--</b></div></div></div>
<div class="tabs"><button class="active" onclick="tab('nav',this)">Navigation</button><button onclick="tab('parcel',this)">Flurstück</button><button onclick="tab('mapPane',this)">Karte</button><button onclick="tab('heightPane',this)">Höhe</button></div>

<div id="nav" class="pane active"><div class="card current"><b>Aktuelle Position</b><div id="autoparcel" class="info">Warte auf GNSS…</div></div><div class="card"><div id="targetname" class="center">Kein Ziel gewählt</div><div id="distance" class="big">--</div><div id="navarrow" class="arrow">↑</div><div id="offset" class="center">--</div></div><div class="card"><button class="primary" onclick="nearestCurrentBoundary()">Nächsten Punkt der ausgewählten Grenze ansteuern</button></div></div>

<div id="parcel" class="pane"><div class="card"><b>Flurstück suchen</b><div class="row" style="margin-top:8px"><select id="kreis" onchange="loadGem()"><option value="">Landkreis…</option></select><select id="gem" onchange="loadFlur()"><option>Gemarkung…</option></select></div><div class="row" style="margin-top:8px"><select id="flur" onchange="loadFls()"><option>Flur…</option></select><select id="fls" onchange="selectParcel()"><option>Flurstück…</option></select></div></div><div class="card" id="parcelinfo">Noch kein Flurstück gewählt.</div><div class="card"><label>Zusätzliche Hilfspunkte:</label><select id="spacing" onchange="loadTargets()"><option value="0">nur ALKIS-Eckpunkte</option><option value="2">alle 2 m</option><option value="5">alle 5 m</option><option value="10">alle 10 m</option></select></div><div class="card targetlist" id="targetlist"></div></div>

<div id="mapPane" class="pane"><div class="card mapfilters"><label><input id="showParcel" type="checkbox" checked onchange="applyMapFilters()"> Flurstück</label><label><input id="showTargets" type="checkbox" checked onchange="applyMapFilters()"> Grenzpunkte</label><label><input id="showHeights" type="checkbox" checked onchange="applyMapFilters()"> Höhenpunkte</label></div><div id="mapdiag" class="card muted">Offlinekarte wird geprüft…</div><div class="mapwrap"><div id="map" class="map"></div></div><div class="muted">Offline-Basiskarte © OpenStreetMap-Mitwirkende. Kataster: © GeoBasis-DE / LVermGeo ST.</div></div>

<div id="heightPane" class="pane">
 <div class="heightnav"><button id="hnavMeasure" class="active" onclick="heightTab('measure')">Messen</button><button id="hnavPoints" onclick="heightTab('points')">Punkte</button><button id="hnavSettings" onclick="heightTab('settings')">⚙ Einstellungen</button></div>
 <div id="hMeasure" class="hsub active">
  <div class="card"><div class="center"><b>Höhenmessung</b></div><div class="heightlive" id="liveheight">--</div><div class="center" id="heightfix">Warte auf RTK…</div><div id="hrefsummary" class="muted center" style="margin-top:6px">Referenz wird geladen…</div></div>
  <div id="hrefneeded" class="card hidden"><b>1 · Referenz festlegen</b><p>Für relative Höhenmessungen brauchst du zuerst einen Bezugspunkt.</p><button class="primary measurebtn" onclick="measureHeight('reference')">REFERENZ MESSEN</button></div>
  <div id="hmeasureprogress" class="card hidden"><div class="progress"><div id="hbar"></div></div><div id="hmsg" class="center">Messung läuft…</div></div>
  <div id="hwork" class="card"><div class="center muted" id="htargetsummary">Soll wird geladen…</div><div id="hlivedelta" class="heightdelta heightneutral">--</div><div id="hdirection" class="direction">Noch kein Sollvergleich</div><div id="htraffic" class="traffic heightneutral">--</div><div class="metricgrid" style="margin-top:8px"><div class="metric">Messqualität<br><b id="hlivequality">--</b></div><div class="metric">Stab / Dauer<br><b id="hquicksettings">--</b></div></div><button id="hmeasure" class="primary measurebtn" onclick="measureHeight('point')">HÖHENPUNKT SPEICHERN</button><div id="hresult" class="resultbox hidden"></div></div>
  <div class="card danger"><b>Hinweis:</b> RTK-GNSS-Höhen sind empfindlicher als die Lage. Für finale mm-Kontrollen an Schalung/Bodenplatte weiterhin Rotationslaser oder Nivellement verwenden.</div>
 </div>
 <div id="hPoints" class="hsub"><div class="card"><div class="row"><button class="secondary" onclick="showAllHeightPoints()">AUF KARTE ZEIGEN</button><button onclick="loadHeights()">Aktualisieren</button></div><div id="heightstats" class="muted" style="margin-top:8px"></div></div><div class="card"><b>Gespeicherte Höhenpunkte</b><div id="heightlist" style="margin-top:8px">Noch keine Punkte.</div></div></div>
 <div id="hSettings" class="hsub">
  <div class="card"><b>Bezug und Sollhöhe</b><div style="margin-top:8px"><label>Messmodus</label><select id="hmode" onchange="syncHeightSettingVisibility()"><option value="AGL">Relative Höhe zu Referenz</option><option value="MSL">Absolute Höhe (MSL)</option></select></div><div id="hrelset" style="margin-top:8px"><label>Soll relativ zu REF [m]</label><input id="hagltarget" type="number" inputmode="decimal" step="0.001" value="0.000"></div><div id="hmslset" style="margin-top:8px"><label>Soll MSL [m]</label><input id="hmsltarget" type="number" inputmode="decimal" step="0.001" placeholder="z.B. 78.450"></div><div style="margin-top:8px"><label>Toleranz ± [mm]</label><select id="htol"><option value="10">±10 mm</option><option value="20" selected>±20 mm</option><option value="30">±30 mm</option><option value="50">±50 mm</option></select></div></div>
  <div class="card"><b>Messung</b><div class="row" style="margin-top:8px"><div><label>Stabhöhe [m]</label><input id="hpole" type="number" inputmode="decimal" step="0.001" value="2.000"></div><div><label>Messdauer</label><select id="hduration"><option value="5">5 s</option><option value="10" selected>10 s</option><option value="15">15 s</option><option value="20">20 s</option><option value="30">30 s</option></select></div></div><div class="settingnote">Diese Werte gelten für neue Messungen und müssen draußen normalerweise nicht ständig geändert werden.</div></div>
  <button class="primary" onclick="saveHeightSettings()">EINSTELLUNGEN SPEICHERN</button><div class="card danger" style="margin-top:12px"><button onclick="clearHeights()">Alle Höhenpunkte löschen</button></div>
 </div>
</div>

<script src="/static/maplibre-gl.js"></script><script>
let selectedParcel=null,targets=[],target=null,map=null,parcelFeature=null,currentMarker=null,targetMarker=null,lastNearE=null,lastNearN=null,targetLabelMarkers=[],heightMarkerObjects=[],heightData=null,lastStatus=null,heightTimer=null,heightMeasuring=false,mapReady=null;
function tab(id,b){document.querySelectorAll('.pane').forEach(x=>x.classList.remove('active'));document.querySelectorAll('.tabs button').forEach(x=>x.classList.remove('active'));document.getElementById(id).classList.add('active');b.classList.add('active');if(id==='mapPane'){initMap();setTimeout(()=>map&&map.resize(),50)}if(id==='heightPane')loadHeights()}
function heightTab(which){document.querySelectorAll('.hsub').forEach(x=>x.classList.remove('active'));document.querySelectorAll('.heightnav button').forEach(x=>x.classList.remove('active'));document.getElementById('h'+which[0].toUpperCase()+which.slice(1)).classList.add('active');document.getElementById('hnav'+which[0].toUpperCase()+which.slice(1)).classList.add('active')}
function cls(el,state){el.className=state?'ok':'bad'}
function fmtD(d){if(d==null)return'--';return d<1?(d*100).toFixed(1)+' cm':d.toFixed(2)+' m'}
function fmtDelta(m){if(m==null)return'--';let mm=Math.round(m*1000);return (mm>0?'+':'')+mm+' mm'}
function heightClass(level){return level==='green'?'heightgreen':(level==='yellow'?'heightyellow':(level==='red'?'heightred':'heightneutral'))}
function qualityLabel(level){return level==='green'?'GUT':(level==='yellow'?'MITTEL':(level==='red'?'SCHLECHT':'--'))}
async function poll(){let q=target?('?E='+target.E+'&N='+target.N):'';let s=await fetch('/api/status'+q).then(r=>r.json());lastStatus=s;wifi.textContent=s.wifi_ssid?('✓ '+s.wifi_ssid):'✗';cls(wifi,!!s.wifi_ssid);inet.textContent=s.internet?'✓ online':'✗ offline';cls(inet,s.internet);let sk=s.ntrip==='RTCM STREAM';sapos.textContent=sk?'✓ RTCM':s.ntrip;sapos.className=sk?'ok':'warn';rtk.textContent=s.fix==='RTK FIX'?'✓ FIX':s.fix;rtk.className=s.fix==='RTK FIX'?'ok':(s.fix==='RTK FLOAT'?'warn':'bad');
 if(s.current_parcels&&s.current_parcels.length){let p=s.current_parcels[0];autoparcel.innerHTML=`<b>${p.gemarkung} · Flur ${p.flur} · ${p.nummer}</b><br>${p.kreis}${p.lage?'<br>'+p.lage:''}<br><button style="margin-top:7px" onclick="loadParcelById(${p.id})">Dieses Flurstück laden</button>`}else autoparcel.innerHTML=s.lat==null?'Warte auf GNSS…':`Kein Flurstück erkannt<br><span class="muted">E ${s.E.toFixed(3)} · N ${s.N.toFixed(3)}</span>`;
 if(s.nav){distance.textContent=fmtD(s.nav.distance);navarrow.textContent=s.nav.arrow;offset.textContent=`${Math.abs(s.nav.dE).toFixed(2)} m ${s.nav.dE>=0?'Ost':'West'} · ${Math.abs(s.nav.dN).toFixed(2)} m ${s.nav.dN>=0?'Nord':'Süd'}`}else{distance.textContent='--';offset.textContent='--'}
 if(map&&s.lon!=null){if(!currentMarker)currentMarker=new maplibregl.Marker({color:'#1d7cff'}).setLngLat([s.lon,s.lat]).addTo(map);else currentMarker.setLngLat([s.lon,s.lat]);let moved=lastNearE==null?999:Math.hypot(s.E-lastNearE,s.N-lastNearN);if(moved>40)refreshNearby(s.E,s.N)}
 updateHeightLive(s)
}
setInterval(poll,1000);poll();
async function loadCounties(){let a=await fetch('/api/counties').then(r=>r.json());a.forEach(x=>kreis.add(new Option(x.kreis,x.kreisschl)))}
async function loadGem(){gem.innerHTML='<option value="">Gemarkung…</option>';flur.innerHTML='<option>Flur…</option>';fls.innerHTML='<option>Flurstück…</option>';let a=await fetch('/api/gemarkungen?kreis='+kreis.value).then(r=>r.json());a.forEach(x=>gem.add(new Option(x.gemarkung,x.gemaschl)))}
async function loadFlur(){flur.innerHTML='<option value="">Flur…</option>';fls.innerHTML='<option>Flurstück…</option>';let a=await fetch('/api/fluren?gemaschl='+gem.value).then(r=>r.json());a.forEach(x=>flur.add(new Option(x,x)))}
async function loadFls(){fls.innerHTML='<option value="">Flurstück…</option>';let a=await fetch(`/api/flurstuecke?gemaschl=${gem.value}&flur=${flur.value}`).then(r=>r.json());a.forEach(x=>fls.add(new Option(x.nummer+(x.lage?' · '+x.lage:''),x.id)))}
function selectParcel(){if(fls.value)loadParcelById(parseInt(fls.value))}
async function loadParcelById(id){let d=await fetch('/api/parcel/'+id).then(r=>r.json());selectedParcel=d.parcel;parcelFeature=d.feature;parcelinfo.innerHTML=`<b>${d.parcel.gemarkung} · Flur ${d.parcel.flur} · Flurstück ${d.parcel.nummer}</b><br>${d.parcel.kreis}<br>Fläche: ${d.parcel.flaeche?Math.round(d.parcel.flaeche).toLocaleString('de-DE')+' m²':'--'}${d.parcel.lage?'<br>Lage: '+d.parcel.lage:''}<br><span class="muted">ALKIS: ${d.parcel.aktualit||'--'}</span>`;await loadTargets();drawParcel()}
async function loadTargets(){if(!selectedParcel)return;targets=await fetch(`/api/parcel/${selectedParcel.id}/targets?spacing=${spacing.value}`).then(r=>r.json());targetlist.innerHTML='';targets.forEach((t,i)=>{t._idx=i;let d=document.createElement('div');d.className='target';d.innerHTML=`<button>${t.name} <span class="muted">${t.kind==='helper'?'Hilfspunkt':'ALKIS-Eckpunkt'}</span></button><button class="go">GO</button>`;d.children[0].onclick=()=>showTargetOnMap(t);d.children[1].onclick=()=>setTarget(t);targetlist.appendChild(d)});drawTargetPoints()}
function setTarget(t){target=t;targetname.textContent=`Ziel: ${t.name||t.display_code||'Punkt'}`;if(map&&t.lon!=null){if(targetMarker)targetMarker.remove();targetMarker=new maplibregl.Marker({color:'#e53935'}).setLngLat([t.lon,t.lat]).addTo(map);map.flyTo({center:[t.lon,t.lat],zoom:19})}document.querySelector('.tabs button').click()}
async function nearestCurrentBoundary(){if(!selectedParcel){alert('Bitte zuerst ein Flurstück wählen oder das erkannte Flurstück laden.');return}let t=await fetch(`/api/nearest-boundary/${selectedParcel.id}`).then(r=>r.json());setTarget({name:'Nächster Grenzpunkt',E:t.E,N:t.N,lon:t.lon,lat:t.lat,kind:'projection'})}
function clearTargetLabels(){targetLabelMarkers.forEach(m=>m.remove());targetLabelMarkers=[]}
function targetFeatureCollection(){return {type:'FeatureCollection',features:targets.map((t,i)=>({type:'Feature',properties:{idx:i,name:t.name,kind:t.kind},geometry:{type:'Point',coordinates:[t.lon,t.lat]}}))}}
function drawTargetPoints(){if(!map||!map.isStyleLoaded())return;let fc=targetFeatureCollection();if(map.getSource('target-points'))map.getSource('target-points').setData(fc);else{map.addSource('target-points',{type:'geojson',data:fc});map.addLayer({id:'target-circles',type:'circle',source:'target-points',paint:{'circle-radius':['case',['==',['get','kind'],'helper'],4,6],'circle-color':['case',['==',['get','kind'],'helper'],'#43a5ff','#ff9800'],'circle-stroke-color':'#111','circle-stroke-width':1.5}})}refreshTargetLabels();applyMapFilters()}
function refreshTargetLabels(){clearTargetLabels();if(!map||map.getZoom()<16||!targets.length||!showTargets.checked)return;let b=map.getBounds(),visible=targets.filter(t=>b.contains([t.lon,t.lat]));visible.slice(0,300).forEach(t=>{let el=document.createElement('div');el.className='pointlabel '+(t.kind==='helper'?'helper':'');el.textContent=t.name;el.onclick=(ev)=>{ev.stopPropagation();showTargetPopup(t)};targetLabelMarkers.push(new maplibregl.Marker({element:el,anchor:'bottom'}).setLngLat([t.lon,t.lat]).addTo(map))})}
function showTargetPopup(t){if(!map)return;let type=t.kind==='helper'?'Hilfspunkt':'ALKIS-Eckpunkt';new maplibregl.Popup({offset:12}).setLngLat([t.lon,t.lat]).setHTML(`<b>${t.name}</b><br>${type}<br>E ${t.E.toFixed(3)}<br>N ${t.N.toFixed(3)}<br><button style="margin-top:7px" onclick="goTargetByIndex(${t._idx})">Diesen Punkt ansteuern</button>`).addTo(map)}
function goTargetByIndex(i){if(targets[i])setTarget(targets[i])}
function showTargetOnMap(t){tab('mapPane',document.querySelectorAll('.tabs button')[2]);setTimeout(()=>{if(map){map.flyTo({center:[t.lon,t.lat],zoom:20});showTargetPopup(t)}},200)}
async function initMap(){if(mapReady)return mapReady;mapReady=createMap();return mapReady}
async function createMap(){if(typeof maplibregl==='undefined'){mapdiag.textContent='FEHLER: MapLibre wurde nicht geladen.';mapdiag.className='card danger';return}let mi=await fetch('/api/map/info').then(r=>r.json()).catch(e=>({exists:false,error:String(e)}));if(!mi.exists){mapdiag.textContent='Offlinekarte fehlt.';mapdiag.className='card danger'}else if(mi.error){mapdiag.textContent='Offlinekarte kann nicht gelesen werden: '+mi.error;mapdiag.className='card danger'}else{mapdiag.textContent=`Offlinekarte OK · ${mi.size_mb} MB · ${mi.tiles.toLocaleString('de-DE')} Kacheln · Zoom ${mi.minzoom}–${mi.maxzoom}`;mapdiag.className='card muted'}map=new maplibregl.Map({container:'map',style:'/api/map/style',center:[11.7,51.9],zoom:8,maxZoom:21});map.addControl(new maplibregl.NavigationControl());map.addControl(new maplibregl.ScaleControl({unit:'metric'}));let ready=new Promise(resolve=>map.on('load',()=>{drawParcel();drawTargetPoints();drawHeightPoints();poll();applyMapFilters();resolve()}));map.on('error',e=>{if(e&&e.error)mapdiag.textContent='Kartenfehler: '+e.error.message});map.on('click','near-lines',e=>{if(e.features&&e.features.length)loadParcelById(parseInt(e.features[0].properties.id))});map.on('mouseenter','near-lines',()=>map.getCanvas().style.cursor='pointer');map.on('mouseleave','near-lines',()=>map.getCanvas().style.cursor='');map.on('moveend',refreshTargetLabels);map.on('zoomend',refreshTargetLabels);map.on('click','target-circles',e=>{if(e.features&&e.features.length){let i=parseInt(e.features[0].properties.idx);if(targets[i])showTargetPopup(targets[i])}});await ready}
async function refreshNearby(E,N){if(!map||!map.isStyleLoaded())return;lastNearE=E;lastNearN=N;let a=await fetch(`/api/parcels/near?E=${E}&N=${N}&r=140`).then(r=>r.json()).catch(()=>[]);let fc={type:'FeatureCollection',features:a.map(p=>({type:'Feature',properties:{id:p.id},geometry:p.geojson}))};if(map.getSource('near'))map.getSource('near').setData(fc);else{map.addSource('near',{type:'geojson',data:fc});map.addLayer({id:'near-lines',type:'line',source:'near',paint:{'line-color':'#606060','line-width':['interpolate',['linear'],['zoom'],15,1,20,2]}})}applyMapFilters()}
function drawParcel(){if(!map||!parcelFeature||!map.isStyleLoaded())return;if(map.getSource('parcel'))map.getSource('parcel').setData(parcelFeature);else{map.addSource('parcel',{type:'geojson',data:parcelFeature});map.addLayer({id:'parcel-fill',type:'fill',source:'parcel',paint:{'fill-color':'#ffcc00','fill-opacity':0.18}});map.addLayer({id:'parcel-line',type:'line',source:'parcel',paint:{'line-color':'#ff9800','line-width':4}})}let coords=[];function walk(a){if(typeof a[0]==='number')coords.push(a);else a.forEach(walk)}walk(parcelFeature.geometry.coordinates);if(coords.length){let b=coords.reduce((bb,p)=>bb.extend(p),new maplibregl.LngLatBounds(coords[0],coords[0]));map.fitBounds(b,{padding:45,maxZoom:19})}applyMapFilters()}
function setLayerVisible(id,on){if(map&&map.getLayer(id))map.setLayoutProperty(id,'visibility',on?'visible':'none')}
function applyMapFilters(){if(!map||!map.isStyleLoaded())return;setLayerVisible('parcel-fill',showParcel.checked);setLayerVisible('parcel-line',showParcel.checked);setLayerVisible('near-lines',showParcel.checked);setLayerVisible('target-circles',showTargets.checked);if(!showTargets.checked)clearTargetLabels();else refreshTargetLabels();heightMarkerObjects.forEach(m=>m.getElement().style.display=showHeights.checked?'flex':'none')}

function syncHeightSettingVisibility(){hrelset.classList.toggle('hidden',hmode.value!=='AGL');hmslset.classList.toggle('hidden',hmode.value!=='MSL')}
function updateHeightLive(s){if(!heightData||typeof liveheight==='undefined')return;let st=heightData.settings||{},pole=parseFloat(st.pole_height_m??2),ground=s.alt==null?null:s.alt-pole,rel=(ground!=null&&heightData.reference_height_m!=null)?ground-heightData.reference_height_m:null;liveheight.textContent=ground==null?'--':(st.mode==='MSL'?ground.toFixed(3)+' m':(rel==null?'REF fehlt':((rel>=0?'+':'')+rel.toFixed(3)+' m')));heightfix.textContent=s.fix+(s.sats?' · '+s.sats+' Sat':'')+(s.hdop!=null?' · HDOP '+Number(s.hdop).toFixed(1):'');heightfix.className='center '+(s.fix==='RTK FIX'?'ok':(s.fix==='RTK FLOAT'?'warn':'bad'));let delta=null;if(st.mode==='MSL'&&ground!=null&&st.target_msl_m!=null)delta=ground-st.target_msl_m;if(st.mode==='AGL'&&rel!=null&&st.target_agl_m!=null)delta=rel-st.target_agl_m;let tol=(st.tolerance_mm||20)/1000,level=delta==null?null:(Math.abs(delta)<=tol?'green':(Math.abs(delta)<=2*tol?'yellow':'red'));hlivedelta.textContent=fmtDelta(delta);hlivedelta.className='heightdelta '+heightClass(level);if(delta==null){hdirection.textContent=st.mode==='AGL'&&heightData.reference_height_m==null?'Zuerst Referenz messen':'Sollhöhe einstellen';htraffic.textContent='Kein Vergleich';htraffic.className='traffic heightneutral'}else{let mm=Math.abs(Math.round(delta*1000));hdirection.textContent=Math.abs(delta)<0.0005?'auf Sollhöhe':(delta>0?`▲ ${mm} mm ZU HOCH`:`▼ ${mm} mm ZU TIEF`);htraffic.textContent=level==='green'?'✓ INNERHALB TOLERANZ':(level==='yellow'?'⚠ KNAPP AUSSERHALB':'✕ AUSSERHALB TOLERANZ');htraffic.className='traffic '+heightClass(level)}hlivequality.textContent=s.fix==='RTK FIX'&&s.age!=null&&s.age<=4?'RTK FIX':'NICHT MESSEN';hlivequality.className=s.fix==='RTK FIX'&&s.age!=null&&s.age<=4?'heightgreen':'heightred'}
async function loadHeights(){let d=await fetch('/api/height/points').then(r=>r.json());heightData=d;let st=d.settings||{};hmode.value=st.mode||'AGL';let tolValue=String(st.tolerance_mm??20);if(!Array.from(htol.options).some(o=>o.value===tolValue))htol.add(new Option('±'+tolValue+' mm',tolValue));htol.value=tolValue;hmsltarget.value=st.target_msl_m==null?'':Number(st.target_msl_m).toFixed(3);hagltarget.value=st.target_agl_m==null?'0.000':Number(st.target_agl_m).toFixed(3);hpole.value=Number(st.pole_height_m??2).toFixed(3);let durationValue=String(st.duration_s??10);if(!Array.from(hduration.options).some(o=>o.value===durationValue))hduration.add(new Option(durationValue+' s',durationValue));hduration.value=durationValue;syncHeightSettingVisibility();hquicksettings.textContent=`${Number(st.pole_height_m??2).toFixed(3)} m · ${Number(st.duration_s||10).toFixed(0)} s`;hrefneeded.classList.toggle('hidden',!(st.mode==='AGL'&&!d.reference_id));hwork.classList.toggle('hidden',st.mode==='AGL'&&!d.reference_id);hrefsummary.textContent=d.reference?`REF · MSL ${d.reference.height_m.toFixed(3)} m · σ ${(d.reference.sigma_m*1000).toFixed(1)} mm`:(st.mode==='MSL'?'Absolute MSL-Messung':'Noch keine Referenz gesetzt');htargetsummary.textContent=st.mode==='MSL'?`Soll MSL ${st.target_msl_m==null?'nicht gesetzt':Number(st.target_msl_m).toFixed(3)+' m'} · ±${st.tolerance_mm} mm`:`Soll relativ ${Number(st.target_agl_m||0)>=0?'+':''}${Number(st.target_agl_m||0).toFixed(3)} m · ±${st.tolerance_mm} mm`;renderHeightList();drawHeightPoints();if(lastStatus)updateHeightLive(lastStatus)}
function renderHeightList(){heightlist.innerHTML='';let pts=(heightData&&heightData.points)||[];if(!pts.length){heightlist.textContent='Noch keine Punkte.';heightstats.textContent='0 Messpunkte';return}let ok=pts.filter(p=>p.level==='green').length,bad=pts.filter(p=>p.level==='red').length;heightstats.textContent=`${pts.length} Messpunkte · ${ok} innerhalb Toleranz · ${bad} deutlich außerhalb`;pts.forEach(p=>{let row=document.createElement('div');row.className='heightrow'+(p.is_reference?' ref':'');let rel=p.agl_m==null?'--':((p.agl_m>=0?'+':'')+p.agl_m.toFixed(3)+' m');row.innerHTML=`<div><b class="${heightClass(p.level)}">${p.display_code}</b>${p.description?' · '+escapeHtml(p.description):''}${p.is_reference?' <span class="badge">Referenz</span>':''}<br><span class="muted">MSL ${p.height_m.toFixed(3)} m · relativ ${rel}<br>Abw. ${fmtDelta(p.active_delta_m)} · Qualität ${qualityLabel(p.quality_level)} · σ ${(p.sigma_m*1000).toFixed(1)} mm</span></div><div><button class="smallbtn secondary" onclick="showHeightOnMap(${p.id})">Karte</button><button class="smallbtn" onclick="editHeightDescription(${p.id})">Name</button><button class="smallbtn" onclick="setHeightRef(${p.id})">REF</button><button class="smallbtn" onclick="deleteHeight(${p.id})">✕</button></div>`;heightlist.appendChild(row)})}
function escapeHtml(s){return String(s||'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))}
async function saveHeightSettings(){let payload={mode:hmode.value,target_msl_m:hmsltarget.value===''?null:parseFloat(hmsltarget.value),target_agl_m:hagltarget.value===''?0:parseFloat(hagltarget.value),tolerance_mm:parseFloat(htol.value||20),pole_height_m:parseFloat(hpole.value||2),duration_s:parseFloat(hduration.value||10)};let r=await fetch('/api/height/settings',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});heightData=await r.json();heightTab('measure');await loadHeights()}
function startProgress(seconds){clearInterval(heightTimer);hmeasureprogress.classList.remove('hidden');hbar.style.width='0%';let start=Date.now();hmsg.textContent='RTK FIX · Stab ruhig und lotrecht halten';heightTimer=setInterval(()=>{let p=Math.min(100,(Date.now()-start)/(seconds*1000)*100);hbar.style.width=p+'%';hmsg.textContent=`Messung läuft · ${Math.min(seconds,Math.floor((Date.now()-start)/1000)+1)} / ${seconds} s`;if(p>=100)clearInterval(heightTimer)},150)}
async function measureHeight(role){if(heightMeasuring)return;let st=(heightData&&heightData.settings)||{},duration=Number(st.duration_s||10),pole=Number(st.pole_height_m??2);if(!lastStatus||lastStatus.fix!=='RTK FIX'||lastStatus.age==null||lastStatus.age>4){alert('Messung nur bei RTK FIX möglich.');return}heightMeasuring=true;let btn=role==='point'?hmeasure:null;if(btn){btn.disabled=true;btn.textContent='MESSUNG LÄUFT…'}startProgress(duration);hresult.classList.add('hidden');try{let r=await fetch('/api/height/capture',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({role:role,pole_height:pole,duration:duration})});let d=await r.json();if(!r.ok)throw new Error(d.error||'Messfehler');heightData=d.data;clearInterval(heightTimer);hbar.style.width='100%';hmsg.textContent='✓ Messung gespeichert';await loadHeights();let p=d.point;hresult.classList.remove('hidden');hresult.innerHTML=`<b>✓ ${p.display_code} gespeichert</b><br>MSL ${p.height_m.toFixed(3)} m${p.agl_m==null?'':` · relativ ${(p.agl_m>=0?'+':'')+p.agl_m.toFixed(3)} m`}<br>Abweichung <b class="${heightClass(p.level)}">${fmtDelta(p.active_delta_m)}</b><br>Messqualität <b class="${heightClass(p.quality_level)}">${qualityLabel(p.quality_level)}</b> · σ ${(p.sigma_m*1000).toFixed(1)} mm<br><div class="row" style="margin-top:8px"><button class="secondary" onclick="showHeightOnMap(${p.id})">AUF KARTE ZEIGEN</button><button onclick="hresult.classList.add('hidden')">NÄCHSTER PUNKT</button></div>`}catch(e){clearInterval(heightTimer);hmsg.textContent=e.message;hbar.style.width='0%'}heightMeasuring=false;if(btn){btn.disabled=false;btn.textContent='HÖHENPUNKT SPEICHERN'}}
async function setHeightRef(id){await fetch('/api/height/reference/'+id,{method:'POST'});await loadHeights()}
async function editHeightDescription(id){let p=heightData&&heightData.points.find(x=>x.id===id);if(!p)return;let v=prompt('Bezeichnung für '+p.display_code+':',p.description||'');if(v===null)return;await fetch('/api/height/point/'+id,{method:'PATCH',headers:{'Content-Type':'application/json'},body:JSON.stringify({description:v})});await loadHeights()}
async function deleteHeight(id){if(confirm('Diesen Höhenpunkt löschen?')){await fetch('/api/height/point/'+id,{method:'DELETE'});await loadHeights()}}
async function clearHeights(){if(confirm('Wirklich ALLE Höhenpunkte löschen?')){await fetch('/api/height/clear',{method:'POST'});await loadHeights();heightTab('measure')}}
function drawHeightPoints(){heightMarkerObjects.forEach(m=>m.remove());heightMarkerObjects=[];if(!map||!heightData||!heightData.points)return;heightData.points.forEach(p=>{if(p.lon==null||p.lat==null)return;let el=document.createElement('div');el.className='hmarker '+(p.is_reference?'ref':(p.level||'neutral'));el.textContent=p.display_code;el.onclick=(ev)=>{ev.stopPropagation();showHeightPopup(p.id)};let m=new maplibregl.Marker({element:el,anchor:'center'}).setLngLat([p.lon,p.lat]).addTo(map);heightMarkerObjects.push(m)});applyMapFilters()}
function showHeightPopup(id){if(!map||!heightData)return;let p=heightData.points.find(x=>x.id===id);if(!p)return;let rel=p.agl_m==null?'--':((p.agl_m>=0?'+':'')+p.agl_m.toFixed(3)+' m');new maplibregl.Popup({offset:20}).setLngLat([p.lon,p.lat]).setHTML(`<b>${p.display_code}${p.description?' · '+escapeHtml(p.description):''}</b><br>MSL ${p.height_m.toFixed(3)} m<br>relativ ${rel}<br>Abweichung <b>${fmtDelta(p.active_delta_m)}</b><br>σ ${(p.sigma_m*1000).toFixed(1)} mm · ${p.duration_s}s · ${p.samples} Werte<br><button style="margin-top:6px" onclick="goHeight(${p.id})">ANSTEUERN</button><button style="margin-top:6px" onclick="setHeightRef(${p.id})">ALS REFERENZ</button><button style="margin-top:6px" onclick="editHeightDescription(${p.id})">BEZEICHNUNG</button><button style="margin-top:6px" onclick="deleteHeight(${p.id})">LÖSCHEN</button>`).addTo(map)}
function goHeight(id){let p=heightData&&heightData.points.find(x=>x.id===id);if(p)setTarget({name:p.display_code,E:p.E,N:p.N,lon:p.lon,lat:p.lat,kind:'height'})}
async function showHeightOnMap(id){let p=heightData&&heightData.points.find(x=>x.id===id);if(!p||p.lon==null||p.lat==null)return;showHeights.checked=true;tab('mapPane',document.querySelectorAll('.tabs button')[2]);await initMap();if(map){applyMapFilters();map.flyTo({center:[p.lon,p.lat],zoom:20});showHeightPopup(id)}}
async function showAllHeightPoints(){let pts=(heightData&&heightData.points||[]).filter(p=>p.lon!=null&&p.lat!=null);if(!pts.length)return;showHeights.checked=true;tab('mapPane',document.querySelectorAll('.tabs button')[2]);await initMap();if(!map)return;applyMapFilters();if(pts.length===1){map.flyTo({center:[pts[0].lon,pts[0].lat],zoom:20});return}let b=new maplibregl.LngLatBounds([pts[0].lon,pts[0].lat],[pts[0].lon,pts[0].lat]);pts.forEach(p=>b.extend([p.lon,p.lat]));map.fitBounds(b,{padding:55,maxZoom:20})}
loadHeights();loadCounties();
</script></body></html>'''


@app.route('/')
def index():
    return render_template_string(HTML)


if __name__ == '__main__':
    threading.Thread(target=serial_worker, daemon=True).start()
    threading.Thread(target=ntrip_worker, daemon=True).start()
    threading.Thread(target=network_worker, daemon=True).start()
    app.run(host='0.0.0.0', port=5000, debug=False, threaded=True)
