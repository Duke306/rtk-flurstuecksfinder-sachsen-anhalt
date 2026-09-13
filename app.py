from flask import Flask, render_template_string, jsonify, request, Response, send_from_directory
from pyproj import Transformer
from parcel_db import ParcelDB
import base64, math, os, socket, sqlite3, subprocess, threading, time, serial, gzip, json, statistics
from collections import deque

BASE=os.path.dirname(os.path.abspath(__file__))
DATA=os.path.join(BASE,'data')
STATIC=os.path.join(BASE,'static')
PARCEL_DB=os.environ.get('RTK_PARCEL_DB',os.path.join(DATA,'alkis_st.sqlite'))
MAP_DB=os.environ.get('RTK_MAP_DB',os.path.join(DATA,'sachsen-anhalt-shortbread-1.0.mbtiles'))
HEIGHT_FILE=os.environ.get('RTK_HEIGHT_FILE',os.path.join(DATA,'height_points.json'))

app=Flask(__name__,static_folder=STATIC)
parcels=ParcelDB(PARCEL_DB) if os.path.exists(PARCEL_DB) else None

SERIAL_BAUD=115200
SERIAL_CANDIDATES=['/dev/serial0','/dev/ttyS0','/dev/ttyAMA0','/dev/ttyUSB0']
NTRIP_HOST=os.environ.get('NTRIP_HOST','4G.sapos-lsa-ntrip.de'); NTRIP_PORT=int(os.environ.get('NTRIP_PORT','2101')); NTRIP_MOUNTPOINT=os.environ.get('NTRIP_MOUNTPOINT','VRS_3_4G_ST'); NTRIP_USER=os.environ.get('NTRIP_USER','user'); NTRIP_PASSWORD=os.environ.get('NTRIP_PASSWORD','user')
SIM_START_E=671730.0; SIM_START_N=5738312.0
state_lock=threading.Lock(); serial_write_lock=threading.Lock(); height_file_lock=threading.Lock()
height_samples=deque(maxlen=1200)
state={'serial_connected':False,'serial_port':None,'serial_error':'','serial_obj':None,'last_nmea':'','last_gga':'','last_fix_time':0.0,
       'lat':None,'lon':None,'alt':None,'geoid_sep':None,'E':SIM_START_E,'N':SIM_START_N,'quality':0,'fix':'SIMULATION','sats':0,'hdop':None,
       'ntrip':'WARTET AUF LC29H','ntrip_error':'','rtcm_bytes':0,'wifi_ssid':'','local_ip':'','internet':False}
geo_to_utm=Transformer.from_crs('EPSG:4258','EPSG:25832',always_xy=True)
utm_to_geo=Transformer.from_crs('EPSG:25832','EPSG:4326',always_xy=True)

# ---------- GNSS / SAPOS ----------
def nmea_degrees(v,h):
    if not v:return None
    raw=float(v); deg=int(raw//100); m=raw-deg*100; r=deg+m/60
    return -r if h in ('S','W') else r

def fix_name(q): return {0:'NO FIX',1:'GNSS',2:'DGPS',3:'PPS',4:'RTK FIX',5:'RTK FLOAT',6:'ESTIMATED'}.get(q,f'FIX {q}')

def parse_gga(line):
    f=line.split('*',1)[0].split(',')
    if len(f)<10 or not f[0].endswith('GGA'):return False
    try:
        lat=nmea_degrees(f[2],f[3]);lon=nmea_degrees(f[4],f[5]);q=int(f[6] or 0);sats=int(f[7] or 0);hdop=float(f[8]) if f[8] else None
        alt=float(f[9]) if f[9] else None
        geoid_sep=float(f[11]) if len(f)>11 and f[11] else None
    except (ValueError,IndexError):return False
    if lat is None or lon is None:return False
    E,N=geo_to_utm.transform(lon,lat);now=time.time()
    sample={'t':now,'quality':q,'fix':fix_name(q),'alt':alt,'E':E,'N':N,'hdop':hdop,'sats':sats,'lat':lat,'lon':lon}
    with state_lock:
        state.update(serial_connected=True,serial_error='',last_gga=line.strip(),last_nmea=line.strip(),last_fix_time=now,lat=lat,lon=lon,alt=alt,geoid_sep=geoid_sep,quality=q,fix=fix_name(q),sats=sats,hdop=hdop,E=E,N=N)
        if alt is not None:height_samples.append(sample)
    return True

def choose_serial_port():
    return next((p for p in SERIAL_CANDIDATES if os.path.exists(p)),None)

def serial_worker():
    while True:
        port=choose_serial_port()
        if not port:
            with state_lock: state.update(serial_connected=False,serial_port=None,serial_obj=None,fix='SIMULATION',serial_error='Kein UART-Gerät gefunden')
            time.sleep(2);continue
        ser=None
        try:
            ser=serial.Serial(port,SERIAL_BAUD,timeout=1.0,write_timeout=2.0);ser.reset_input_buffer()
            with state_lock: state.update(serial_port=port,serial_obj=ser,serial_error='UART offen, warte auf NMEA')
            last=0
            while True:
                raw=ser.readline()
                if not raw:
                    if last and time.time()-last>4:
                        with state_lock: state.update(serial_connected=False,fix='NO DATA')
                    continue
                line=raw.decode('ascii',errors='ignore').strip()
                if not line.startswith('$'):continue
                last=time.time()
                with state_lock: state.update(last_nmea=line,serial_error='')
                if line.split('*',1)[0].split(',',1)[0].endswith('GGA'):parse_gga(line)
        except Exception as e:
            with state_lock: state.update(serial_connected=False,serial_obj=None,serial_error=str(e),fix='SIMULATION')
            try:
                if ser:ser.close()
            except:pass
            time.sleep(2)

def get_wifi_ssid():
    try:
        r=subprocess.run(['nmcli','-t','-f','ACTIVE,SSID','dev','wifi'],capture_output=True,text=True,timeout=2)
        for line in r.stdout.splitlines():
            if line.startswith('yes:'):return line.split(':',1)[1] or '(verbunden)'
    except:pass
    return ''
def get_local_ip():
    try:
        ips=subprocess.run(['hostname','-I'],capture_output=True,text=True,timeout=2).stdout.strip().split();return ips[0] if ips else ''
    except:return ''
def internet_ok():
    try:s=socket.create_connection(('1.1.1.1',443),timeout=1.5);s.close();return True
    except:return False

def network_worker():
    while True:
        with state_lock: state.update(wifi_ssid=get_wifi_ssid(),local_ip=get_local_ip(),internet=internet_ok())
        time.sleep(3)

def ntrip_request():
    auth=base64.b64encode(f'{NTRIP_USER}:{NTRIP_PASSWORD}'.encode()).decode()
    return (f'GET /{NTRIP_MOUNTPOINT} HTTP/1.1\r\nHost: {NTRIP_HOST}:{NTRIP_PORT}\r\nNtrip-Version: Ntrip/2.0\r\nUser-Agent: NTRIP RTK-Rover-ST/2.0\r\nAuthorization: Basic {auth}\r\nConnection: close\r\n\r\n').encode()
def get_gga_bytes():
    with state_lock:g=state.get('last_gga','')
    return (g+'\r\n').encode() if g else None
def write_rtcm(data):
    if not data:return
    with state_lock:ser=state.get('serial_obj')
    if not ser:return
    try:
        with serial_write_lock:ser.write(data)
        with state_lock:state['rtcm_bytes']+=len(data)
    except Exception as e:
        with state_lock:state['ntrip_error']=f'RTCM -> GNSS: {e}'

def ntrip_worker():
    while True:
        with state_lock:connected=state['serial_connected'];gga=bool(state['last_gga'])
        if not connected:
            with state_lock:state['ntrip']='WARTET AUF LC29H'
            time.sleep(2);continue
        if not gga:
            with state_lock:state['ntrip']='WARTET AUF POSITION'
            time.sleep(1);continue
        sock=None
        try:
            with state_lock:state.update(ntrip='VERBINDET',ntrip_error='')
            sock=socket.create_connection((NTRIP_HOST,NTRIP_PORT),timeout=10);sock.sendall(ntrip_request())
            resp=b''
            while b'\r\n\r\n' not in resp and len(resp)<16384:
                c=sock.recv(4096)
                if not c:raise ConnectionError('Caster hat Verbindung beendet')
                resp+=c
            head,_,rest=resp.partition(b'\r\n\r\n');first=head.split(b'\r\n',1)[0].decode(errors='ignore')
            if '200' not in first and not first.startswith('ICY 200'):raise ConnectionError(first)
            if rest:write_rtcm(rest)
            g=get_gga_bytes()
            if g:sock.sendall(g)
            sock.settimeout(1);last=time.time()
            with state_lock:state['ntrip']='RTCM STREAM'
            while True:
                with state_lock:
                    if not state['serial_connected']:raise ConnectionError('GNSS-Daten abgebrochen')
                if time.time()-last>=5:
                    g=get_gga_bytes()
                    if g:sock.sendall(g)
                    last=time.time()
                try:
                    d=sock.recv(4096)
                    if not d:raise ConnectionError('RTCM-Stream beendet')
                    write_rtcm(d)
                except socket.timeout:pass
        except Exception as e:
            with state_lock:state.update(ntrip='GETRENNT',ntrip_error=str(e))
            time.sleep(3)
        finally:
            try:
                if sock:sock.close()
            except:pass

def snap():
    with state_lock:return {k:v for k,v in state.items() if k!='serial_obj'}

def arrow(b):return ['↑','↗','→','↘','↓','↙','←','↖'][round(b/45)%8]

# ---------- Offline MBTiles ----------
map_local=threading.local()
def map_conn():
    c=getattr(map_local,'c',None)
    if c is None and os.path.exists(MAP_DB):
        c=sqlite3.connect(f'file:{MAP_DB}?mode=ro',uri=True,check_same_thread=False);map_local.c=c
    return c

@app.route('/tiles/<int:z>/<int:x>/<int:y>.pbf')
def tile(z,x,y):
    c=map_conn()
    if not c:return Response(status=404)
    tms=(1<<z)-1-y
    r=c.execute('SELECT tile_data FROM tiles WHERE zoom_level=? AND tile_column=? AND tile_row=?',(z,x,tms)).fetchone()
    if not r:return Response(status=204)
    data=r[0];resp=Response(data,mimetype='application/x-protobuf');resp.headers['Cache-Control']='public,max-age=86400'
    if data[:2]==b'\x1f\x8b':resp.headers['Content-Encoding']='gzip'
    return resp

MAP_STYLE={
 'version':8,
 'sources':{'osm':{'type':'vector','tiles':['/tiles/{z}/{x}/{y}.pbf'],'minzoom':0,'maxzoom':14,'attribution':'© OpenStreetMap contributors'}},
 'layers':[
  {'id':'bg','type':'background','paint':{'background-color':'#eef1e6'}},
  {'id':'land','type':'fill','source':'osm','source-layer':'land','minzoom':7,'paint':{'fill-color':['match',['get','kind'],'forest','#d6e7c5','wood','#d6e7c5','farmland','#ece5c7','meadow','#e3eccf','grass','#e3eccf','residential','#e6e1d8','industrial','#ddd7d0','#e2e5d4'],'fill-opacity':0.85}},
  {'id':'sites','type':'fill','source':'osm','source-layer':'sites','minzoom':12,'paint':{'fill-color':'#ddd7c8','fill-opacity':0.65}},
  {'id':'water','type':'fill','source':'osm','source-layer':'water_polygons','minzoom':4,'paint':{'fill-color':'#a9cfe8'}},
  {'id':'waterline','type':'line','source':'osm','source-layer':'water_lines','minzoom':9,'paint':{'line-color':'#83b9dd','line-width':1.3}},
  {'id':'boundaries','type':'line','source':'osm','source-layer':'boundaries','paint':{'line-color':'#999','line-width':1,'line-dasharray':[3,2]}},
  {'id':'roads-low','type':'line','source':'osm','source-layer':'streets_low','minzoom':5,'maxzoom':11,'paint':{'line-color':'#c0aa86','line-width':['interpolate',['linear'],['zoom'],5,0.4,10,2.2]}},
  {'id':'roads-med','type':'line','source':'osm','source-layer':'streets_med','minzoom':8,'maxzoom':14,'paint':{'line-color':'#bbb09e','line-width':['interpolate',['linear'],['zoom'],8,0.6,14,3.5]}},
  {'id':'streets-outline','type':'line','source':'osm','source-layer':'streets','minzoom':10,'paint':{'line-color':'#aaa','line-width':['interpolate',['linear'],['zoom'],10,1.4,14,4.8]}},
  {'id':'streets','type':'line','source':'osm','source-layer':'streets','minzoom':10,'paint':{'line-color':'#fff','line-width':['interpolate',['linear'],['zoom'],10,0.8,14,3.5]}},
  {'id':'buildings','type':'fill','source':'osm','source-layer':'buildings','minzoom':14,'paint':{'fill-color':'#cdbdaf','fill-outline-color':'#9f9185'}}
 ]
}
@app.route('/api/map/style')
def map_style():
    # MapLibre Request() requires an absolute tile URL in some browsers.
    style=json.loads(json.dumps(MAP_STYLE))
    style['sources']['osm']['tiles']=[request.url_root.rstrip('/')+'/tiles/{z}/{x}/{y}.pbf']
    return jsonify(style)

@app.route('/api/map/info')
def map_info():
    info={'exists':os.path.exists(MAP_DB),'path':MAP_DB,'size_mb':0,'tiles':0,'minzoom':None,'maxzoom':None,'error':''}
    if not info['exists']:return jsonify(info)
    try:
        info['size_mb']=round(os.path.getsize(MAP_DB)/1024/1024,1)
        c=sqlite3.connect(f'file:{MAP_DB}?mode=ro',uri=True)
        try:
            r=c.execute('SELECT COUNT(*),MIN(zoom_level),MAX(zoom_level) FROM tiles').fetchone()
            if r: info['tiles'],info['minzoom'],info['maxzoom']=int(r[0]),r[1],r[2]
        finally:c.close()
    except Exception as e:info['error']=str(e)
    return jsonify(info)

# ---------- Parcel APIs ----------
def db_required():return parcels is not None and os.path.exists(PARCEL_DB)
@app.route('/api/dbinfo')
def dbinfo():
    return jsonify(alkis=db_required(),map=os.path.exists(MAP_DB),alkis_path=PARCEL_DB,map_path=MAP_DB)
@app.route('/api/counties')
def counties():return jsonify(parcels.counties() if db_required() else [])
@app.route('/api/gemarkungen')
def gemarkungen():return jsonify(parcels.gemarkungen(request.args.get('kreis','')) if db_required() else [])
@app.route('/api/fluren')
def fluren():return jsonify(parcels.fluren(request.args.get('gemaschl','')) if db_required() else [])
@app.route('/api/flurstuecke')
def flurstuecke():
    if not db_required():return jsonify([])
    try:return jsonify(parcels.flurstuecke(request.args['gemaschl'],request.args['flur']))
    except:return jsonify([])
@app.route('/api/parcel/<int:pid>')
def parcel(pid):
    if not db_required():return jsonify(error='Keine ALKIS-Datenbank'),404
    p=parcels.get(pid,False);gj=parcels.parcel_geojson(pid)
    if not p:return jsonify(error='nicht gefunden'),404
    return jsonify(parcel=p,feature=gj)
@app.route('/api/parcel/<int:pid>/targets')
def parcel_targets(pid):
    if not db_required():return jsonify([])
    try:spacing=float(request.args.get('spacing','0'))
    except:spacing=0
    ts=parcels.targets(pid,spacing)
    for t in ts:
        t['lon'],t['lat']=utm_to_geo.transform(t['E'],t['N'])
    return jsonify(ts)
@app.route('/api/parcels/near')
def parcels_near():
    if not db_required():return jsonify([])
    try:E=float(request.args['E']);N=float(request.args['N']);r=min(float(request.args.get('r',120)),500)
    except:return jsonify([])
    return jsonify(parcels.near(E,N,r))

# ---------- Rover / navigation ----------
@app.route('/api/status')
def status():
    s=snap();sim=not s['serial_connected'];E=s['E'];N=s['N'];targetE=request.args.get('E');targetN=request.args.get('N')
    nav=None
    if targetE is not None and targetN is not None:
        try:
            te=float(targetE);tn=float(targetN);de=te-E;dn=tn-N;d=math.hypot(de,dn);b=math.degrees(math.atan2(de,dn))%360
            nav={'distance':d,'dE':de,'dN':dn,'bearing':b,'arrow':arrow(b),'target_E':te,'target_N':tn}
        except:pass
    current=[]
    if db_required() and s['lat'] is not None:
        try:current=parcels.at_point(E,N)
        except:current=[]
    age=time.time()-s['last_fix_time'] if s['last_fix_time'] else None
    return jsonify(simulation=sim,fix='SIMULATION' if sim else s['fix'],E=E,N=N,lat=s['lat'],lon=s['lon'],alt=s['alt'],geoid_sep=s.get('geoid_sep'),sats=s['sats'],hdop=s['hdop'],age=age,
      serial_port=s['serial_port'],serial_error=s['serial_error'],wifi_ssid=s['wifi_ssid'],local_ip=s['local_ip'],internet=s['internet'],ntrip=s['ntrip'],ntrip_error=s['ntrip_error'],rtcm_bytes=s['rtcm_bytes'],current_parcels=current,nav=nav)

@app.route('/api/nearest-boundary/<int:pid>')
def nearest_boundary(pid):
    if not db_required():return jsonify(error='Keine ALKIS-DB'),404
    s=snap();p=parcels.get(pid,True)
    if not p:return jsonify(error='nicht gefunden'),404
    d,pt=parcels.nearest_boundary_point(s['E'],s['N'],p['polys']);lon,lat=utm_to_geo.transform(*pt)
    return jsonify(distance=d,E=pt[0],N=pt[1],lon=lon,lat=lat)


# ---------- Höhenmessung v5: MSL + lokales AGL ----------
def _height_defaults():
    return {
        'reference_id':None,
        'points':[],
        'settings':{
            'mode':'AGL',          # AGL or MSL controls the traffic light
            'target_msl_m':None,   # absolute orthometric height
            'target_agl_m':0.0,    # relative to selected reference point
            'tolerance_mm':20.0
        }
    }

def _height_store_load():
    with height_file_lock:
        try:
            with open(HEIGHT_FILE,'r',encoding='utf-8') as f:d=json.load(f)
            if not isinstance(d,dict):raise ValueError()
        except Exception:
            d=_height_defaults()
        d.setdefault('reference_id',None);d.setdefault('points',[]);d.setdefault('settings',{})
        st=d['settings']
        st.setdefault('mode','AGL');st.setdefault('target_msl_m',None);st.setdefault('target_agl_m',0.0);st.setdefault('tolerance_mm',20.0)
        if st.get('mode') not in ('AGL','MSL'):st['mode']='AGL'
        return d

def _height_store_save(d):
    os.makedirs(DATA,exist_ok=True)
    tmp=HEIGHT_FILE+'.tmp'
    with height_file_lock:
        with open(tmp,'w',encoding='utf-8') as f:json.dump(d,f,ensure_ascii=False,indent=2)
        os.replace(tmp,HEIGHT_FILE)

def _height_level(delta_m,tolerance_mm):
    if delta_m is None:return None
    tol=max(0.001,float(tolerance_mm))/1000.0
    a=abs(delta_m)
    if a<=tol:return 'green'
    if a<=2*tol:return 'yellow'
    return 'red'

def _height_result():
    d=_height_store_load();ref=None
    for p in d['points']:
        if str(p.get('id'))==str(d.get('reference_id')):ref=p;break
    st=d['settings'];tol=st.get('tolerance_mm',20.0)
    target_msl=st.get('target_msl_m');target_agl=st.get('target_agl_m')
    for p in d['points']:
        p['is_reference']=bool(ref and str(p.get('id'))==str(ref.get('id')))
        p['agl_m']=(p['height_m']-ref['height_m']) if ref else None
        p['delta_reference_m']=p['agl_m']
        p['delta_msl_m']=(p['height_m']-float(target_msl)) if target_msl is not None else None
        p['delta_agl_m']=(p['agl_m']-float(target_agl)) if p['agl_m'] is not None and target_agl is not None else None
        active=p['delta_msl_m'] if st.get('mode')=='MSL' else p['delta_agl_m']
        p['active_delta_m']=active
        p['level']=_height_level(active,tol)
    d['reference_height_m']=ref['height_m'] if ref else None
    return d

@app.route('/api/height/points')
def height_points():return jsonify(_height_result())

@app.route('/api/height/settings',methods=['GET','POST'])
def height_settings():
    d=_height_store_load()
    if request.method=='POST':
        cfg=request.get_json(silent=True) or {};st=d['settings']
        mode=str(cfg.get('mode',st.get('mode','AGL'))).upper()
        if mode in ('AGL','MSL'):st['mode']=mode
        def optional_float(v,old):
            if v in (None,''):return None
            try:return float(v)
            except:return old
        if 'target_msl_m' in cfg:st['target_msl_m']=optional_float(cfg.get('target_msl_m'),st.get('target_msl_m'))
        if 'target_agl_m' in cfg:st['target_agl_m']=optional_float(cfg.get('target_agl_m'),st.get('target_agl_m'))
        if st.get('target_agl_m') is None:st['target_agl_m']=0.0
        if 'tolerance_mm' in cfg:
            try:st['tolerance_mm']=max(1.0,min(200.0,float(cfg.get('tolerance_mm'))))
            except:pass
        _height_store_save(d)
    return jsonify(_height_result())

@app.route('/api/height/capture',methods=['POST'])
def height_capture():
    cfg=request.get_json(silent=True) or {}
    name=str(cfg.get('name') or '').strip()[:60] or f"Punkt {int(time.time())}"
    try:duration=max(3.0,min(60.0,float(cfg.get('duration',10))))
    except:duration=10.0
    try:pole=max(0.0,min(10.0,float(cfg.get('pole_height',2.0))))
    except:pole=2.0

    s=snap()
    if s.get('fix')!='RTK FIX' or s.get('alt') is None:
        return jsonify(error='Höhenmessung nur bei RTK FIX möglich.'),409

    start=time.time()
    while time.time()<start+duration:time.sleep(0.15)

    with state_lock:
        samples=[dict(x) for x in height_samples if x['t']>=start and x['quality']==4 and x['alt'] is not None]
    if len(samples)<max(3,int(duration*0.5)):
        return jsonify(error=f'Zu wenige gültige RTK-FIX-Höhenwerte ({len(samples)}). Bitte erneut messen.'),409

    # NMEA-GGA field 9 is orthometric altitude (MSL/geoid). Correct it by antenna/pole height.
    raw_h=[x['alt']-pole for x in samples]
    med=statistics.median(raw_h)
    good=[(x,h) for x,h in zip(samples,raw_h) if abs(h-med)<=0.08]
    if len(good)<3:good=list(zip(samples,raw_h))
    hs=[h for _,h in good];es=[x['E'] for x,_ in good];ns=[x['N'] for x,_ in good]
    mean_h=statistics.fmean(hs);sigma=statistics.stdev(hs) if len(hs)>1 else 0.0;spread=max(hs)-min(hs) if hs else 0.0

    d=_height_store_load();new_id=max([int(p.get('id',0)) for p in d['points']] or [0])+1
    p={'id':new_id,'name':name,'height_m':mean_h,'antenna_alt_m':mean_h+pole,'pole_height_m':pole,
       'E':statistics.fmean(es),'N':statistics.fmean(ns),'samples':len(hs),'sigma_m':sigma,'spread_m':spread,
       'duration_s':duration,'created':time.strftime('%Y-%m-%d %H:%M:%S'),'fix':'RTK FIX'}
    d['points'].append(p)
    if d.get('reference_id') is None:d['reference_id']=new_id
    _height_store_save(d)
    result=_height_result();saved=next((x for x in result['points'] if x['id']==new_id),p)
    return jsonify(ok=True,point=saved,data=result)

@app.route('/api/height/reference/<int:pid>',methods=['POST'])
def height_reference(pid):
    d=_height_store_load()
    if not any(int(p.get('id',-1))==pid for p in d['points']):return jsonify(error='Punkt nicht gefunden'),404
    d['reference_id']=pid;_height_store_save(d);return jsonify(ok=True,data=_height_result())

@app.route('/api/height/point/<int:pid>',methods=['DELETE'])
def height_delete(pid):
    d=_height_store_load();d['points']=[p for p in d['points'] if int(p.get('id',-1))!=pid]
    if str(d.get('reference_id'))==str(pid):d['reference_id']=d['points'][0]['id'] if d['points'] else None
    _height_store_save(d);return jsonify(ok=True,data=_height_result())

@app.route('/api/height/clear',methods=['POST'])
def height_clear():
    d=_height_store_load();d['reference_id']=None;d['points']=[];_height_store_save(d);return jsonify(ok=True)

HTML=r'''<!doctype html><html lang="de"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1,user-scalable=no">
<title>RTK Flurstücksfinder Sachsen-Anhalt</title><link rel="stylesheet" href="/static/maplibre-gl.css">
<style>
*{box-sizing:border-box}body{margin:0;font-family:Arial,sans-serif;background:#111;color:#eee}.top{padding:10px 12px;background:#181818;position:sticky;top:0;z-index:20}.title{font-size:20px;font-weight:bold}.checks{display:grid;grid-template-columns:repeat(4,1fr);gap:4px;margin-top:8px}.check{background:#272727;border-radius:6px;padding:6px;font-size:11px;text-align:center}.ok{color:#71e28b}.bad{color:#ff7777}.warn{color:#ffd166}.tabs{display:flex;position:sticky;top:77px;background:#111;z-index:19}.tabs button{flex:1;padding:11px;background:#222;color:white;border:0;border-bottom:2px solid #444}.tabs button.active{background:#333;border-bottom-color:#6bc5ff}.pane{display:none;padding:12px}.pane.active{display:block}.card{background:#1d1d1d;border-radius:9px;padding:12px;margin-bottom:10px}.row{display:grid;grid-template-columns:1fr 1fr;gap:8px}select,button,input{font-size:16px;padding:10px;border-radius:7px;border:1px solid #555;background:#292929;color:#fff;width:100%}button.primary{background:#176b2c}.big{font-size:54px;font-weight:bold;text-align:center}.arrow{font-size:80px;text-align:center;line-height:90px}.center{text-align:center}.muted{color:#aaa;font-size:12px}.targetlist{max-height:42vh;overflow:auto}.target{display:flex;gap:7px;align-items:center;padding:6px 0;border-bottom:1px solid #333}.target button{width:auto;flex:1;text-align:left}.target .go{max-width:75px;text-align:center;background:#285b8f}.current{border:1px solid #176b2c}.mapwrap{height:calc(100vh - 150px);min-height:430px}.map{height:100%;border-radius:8px}.pill{display:inline-block;padding:4px 8px;border-radius:12px;background:#333;margin:2px}.info{line-height:1.55}.danger{color:#ff9c9c}.heightrow{display:grid;grid-template-columns:1.25fr .8fr .8fr;gap:6px;align-items:center;padding:8px 0;border-bottom:1px solid #333}.heightdelta{font-size:22px;font-weight:bold}.ref{border:1px solid #ffd166}.measuring{background:#8a5a00!important}.smallbtn{padding:7px;font-size:13px}.heightlive{font-size:38px;font-weight:bold;text-align:center}.pointlabel{font:700 12px Arial;background:#101010dd;color:#fff;border:2px solid #f6a000;border-radius:12px;padding:2px 5px;white-space:nowrap;cursor:pointer;box-shadow:0 1px 3px #0008}.pointlabel.helper{border-color:#43a5ff;font-size:11px}.heightgreen{color:#71e28b}.heightyellow{color:#ffd166}.heightred{color:#ff7777}.heightneutral{color:#bbb}.traffic{font-size:25px;font-weight:bold;text-align:center;padding:8px;border-radius:8px;background:#292929}.switchrow{display:grid;grid-template-columns:1fr 1fr 1fr;gap:6px}
</style></head><body>
<div class="top"><div class="title">RTK Flurstücksfinder · Sachsen-Anhalt</div><div class="checks"><div class="check">WLAN<br><b id="wifi">--</b></div><div class="check">Internet<br><b id="inet">--</b></div><div class="check">SAPOS<br><b id="sapos">--</b></div><div class="check">RTK<br><b id="rtk">--</b></div></div></div>
<div class="tabs"><button class="active" onclick="tab('nav',this)">Navigation</button><button onclick="tab('parcel',this)">Flurstück</button><button onclick="tab('mapPane',this)">Karte</button><button onclick="tab('heightPane',this)">Höhe</button></div>

<div id="nav" class="pane active">
 <div class="card current"><b>Aktuelle Position</b><div id="autoparcel" class="info">Warte auf GNSS…</div></div>
 <div class="card"><div id="targetname" class="center">Kein Ziel gewählt</div><div id="distance" class="big">--</div><div id="navarrow" class="arrow">↑</div><div id="offset" class="center">--</div></div>
 <div class="card"><button class="primary" onclick="nearestCurrentBoundary()">Nächsten Punkt der ausgewählten Grenze ansteuern</button></div>
</div>

<div id="parcel" class="pane">
 <div class="card"><b>Flurstück suchen</b><div class="row" style="margin-top:8px"><select id="kreis" onchange="loadGem()"><option value="">Landkreis…</option></select><select id="gem" onchange="loadFlur()"><option>Gemarkung…</option></select></div><div class="row" style="margin-top:8px"><select id="flur" onchange="loadFls()"><option>Flur…</option></select><select id="fls" onchange="selectParcel()"><option>Flurstück…</option></select></div></div>
 <div class="card" id="parcelinfo">Noch kein Flurstück gewählt.</div>
 <div class="card"><label>Zusätzliche Hilfspunkte:</label><select id="spacing" onchange="loadTargets()"><option value="0">nur ALKIS-Eckpunkte</option><option value="2">alle 2 m</option><option value="5">alle 5 m</option><option value="10">alle 10 m</option></select></div>
 <div class="card targetlist" id="targetlist"></div>
</div>

<div id="mapPane" class="pane"><div id="mapdiag" class="card muted">Offlinekarte wird geprüft…</div><div class="mapwrap"><div id="map" class="map"></div></div><div class="muted">Offline-Basiskarte © OpenStreetMap-Mitwirkende. Kataster: © GeoBasis-DE / LVermGeo ST; technisch aufbereitet für Offline-Nutzung.</div></div>

<div id="heightPane" class="pane">
 <div class="card">
  <b>Höhenmessung · RTK FIX</b>
  <div class="heightlive" id="liveheight">--</div>
  <div class="center" id="heightfix">Warte auf RTK…</div>
  <div class="muted" style="margin-top:8px">MSL = orthometrische GGA-Höhe über Geoid/Meeresspiegel. AGL = lokaler relativer Bezug zu deinem gewählten REF-Punkt.</div>
 </div>
 <div class="card">
  <b>Sollhöhe / Bezug</b>
  <div class="row" style="margin-top:8px"><div><label>Aktiver Bezug</label><select id="hmode"><option value="AGL">AGL · relativ zu REF</option><option value="MSL">MSL · absolute Höhe</option></select></div><div><label>Toleranz ± [mm]</label><input id="htol" type="number" min="1" max="200" step="1" value="20"></div></div>
  <div class="row" style="margin-top:8px"><div><label>Soll MSL [m]</label><input id="hmsltarget" type="number" inputmode="decimal" step="0.001" placeholder="z.B. 78.450"></div><div><label>Soll AGL zu REF [m]</label><input id="hagltarget" type="number" inputmode="decimal" step="0.001" value="0.000"></div></div>
  <button class="primary" style="margin-top:9px" onclick="saveHeightSettings()">Sollhöhe speichern</button>
  <div id="htraffic" class="traffic heightneutral" style="margin-top:9px">Noch kein Vergleich</div>
 </div>
 <div class="card">
  <div class="row"><div><label>Punktname</label><input id="hname" value="Punkt 1"></div><div><label>Stabhöhe [m]</label><input id="hpole" type="number" inputmode="decimal" step="0.001" value="2.000"></div></div>
  <div style="margin-top:8px"><label>Messdauer</label><select id="hduration"><option value="5">5 s</option><option value="10" selected>10 s</option><option value="15">15 s</option><option value="20">20 s</option><option value="30">30 s</option></select></div>
  <button id="hmeasure" class="primary" style="margin-top:10px" onclick="measureHeight()">Punkt messen</button>
  <div id="hmsg" class="center muted" style="margin-top:8px"></div>
 </div>
 <div class="card"><b>Gespeicherte Höhenpunkte</b><div id="heightlist" style="margin-top:8px">Noch keine Punkte.</div></div>
 <div class="card danger"><b>Hinweis:</b> RTK-GNSS-Höhen sind deutlich empfindlicher als die Lage. Für die finale mm-Kontrolle einer Schalung/Bodenplatte weiterhin Rotationslaser oder Nivellement verwenden.</div>
 <button class="smallbtn" onclick="clearHeights()">Alle Höhenpunkte löschen</button>
</div>

<script src="/static/maplibre-gl.js"></script><script>
let selectedParcel=null,targets=[],target=null,map=null,parcelFeature=null,currentMarker=null,targetMarker=null,lastNearE=null,lastNearN=null,targetLabelMarkers=[],heightData=null;
function tab(id,b){document.querySelectorAll('.pane').forEach(x=>x.classList.remove('active'));document.querySelectorAll('.tabs button').forEach(x=>x.classList.remove('active'));document.getElementById(id).classList.add('active');b.classList.add('active');if(id==='mapPane'){initMap();setTimeout(()=>map&&map.resize(),50)}}
function cls(el,state){el.className=state?'ok':'bad'}
function fmtD(d){if(d==null)return'--';return d<1?(d*100).toFixed(1)+' cm':d.toFixed(2)+' m'}
async function poll(){let q=target?('?E='+target.E+'&N='+target.N):'';let s=await fetch('/api/status'+q).then(r=>r.json());wifi.textContent=s.wifi_ssid?('✓ '+s.wifi_ssid):'✗';cls(wifi,!!s.wifi_ssid);inet.textContent=s.internet?'✓ online':'✗ offline';cls(inet,s.internet);let sk=s.ntrip==='RTCM STREAM';sapos.textContent=sk?'✓ RTCM':s.ntrip; sapos.className=sk?'ok':'warn';rtk.textContent=s.fix==='RTK FIX'?'✓ FIX':s.fix;rtk.className=s.fix==='RTK FIX'?'ok':(s.fix==='RTK FLOAT'?'warn':'bad');
 if(s.current_parcels&&s.current_parcels.length){let p=s.current_parcels[0];autoparcel.innerHTML=`<b>${p.gemarkung} · Flur ${p.flur} · ${p.nummer}</b><br>${p.kreis}${p.lage?'<br>'+p.lage:''}<br><button style="margin-top:7px" onclick="loadParcelById(${p.id})">Dieses Flurstück laden</button>`}else autoparcel.innerHTML=s.lat==null?'Warte auf GNSS…':`Kein Flurstück erkannt<br><span class="muted">E ${s.E.toFixed(3)} · N ${s.N.toFixed(3)}</span>`;
 if(s.nav){distance.textContent=fmtD(s.nav.distance);navarrow.textContent=s.nav.arrow;offset.textContent=`${Math.abs(s.nav.dE).toFixed(2)} m ${s.nav.dE>=0?'Ost':'West'} · ${Math.abs(s.nav.dN).toFixed(2)} m ${s.nav.dN>=0?'Nord':'Süd'}`}else{distance.textContent='--';offset.textContent='--'}
 if(map&&s.lon!=null){if(!currentMarker)currentMarker=new maplibregl.Marker({color:'#1d7cff'}).setLngLat([s.lon,s.lat]).addTo(map);else currentMarker.setLngLat([s.lon,s.lat]);let moved=lastNearE==null?999:Math.hypot(s.E-lastNearE,s.N-lastNearN);if(moved>40)refreshNearby(s.E,s.N)}
 if(typeof liveheight!=='undefined'){
  let pole=parseFloat(hpole.value||'0');let ground=(s.alt==null?null:s.alt-pole);let agl=(ground!=null&&heightData&&heightData.reference_height_m!=null)?ground-heightData.reference_height_m:null;
  liveheight.textContent=ground==null?'--':('MSL '+ground.toFixed(3)+' m'+(agl==null?'':' · AGL '+(agl>=0?'+':'')+agl.toFixed(3)+' m'));
  heightfix.textContent=s.fix+(s.hdop!=null?' · HDOP '+s.hdop.toFixed(1):'');heightfix.className='center '+(s.fix==='RTK FIX'?'ok':(s.fix==='RTK FLOAT'?'warn':'bad'));
  updateLiveTraffic(ground,agl);
 }
}
setInterval(poll,1000);poll();
async function loadCounties(){let a=await fetch('/api/counties').then(r=>r.json());a.forEach(x=>kreis.add(new Option(x.kreis,x.kreisschl)))}
async function loadGem(){gem.innerHTML='<option value="">Gemarkung…</option>';flur.innerHTML='<option>Flur…</option>';fls.innerHTML='<option>Flurstück…</option>';let a=await fetch('/api/gemarkungen?kreis='+kreis.value).then(r=>r.json());a.forEach(x=>gem.add(new Option(x.gemarkung,x.gemaschl)))}
async function loadFlur(){flur.innerHTML='<option value="">Flur…</option>';fls.innerHTML='<option>Flurstück…</option>';let a=await fetch('/api/fluren?gemaschl='+gem.value).then(r=>r.json());a.forEach(x=>flur.add(new Option(x,x)))}
async function loadFls(){fls.innerHTML='<option value="">Flurstück…</option>';let a=await fetch(`/api/flurstuecke?gemaschl=${gem.value}&flur=${flur.value}`).then(r=>r.json());a.forEach(x=>fls.add(new Option(x.nummer+(x.lage?' · '+x.lage:''),x.id)))}
function selectParcel(){if(fls.value)loadParcelById(parseInt(fls.value))}
async function loadParcelById(id){let d=await fetch('/api/parcel/'+id).then(r=>r.json());selectedParcel=d.parcel;parcelFeature=d.feature;parcelinfo.innerHTML=`<b>${d.parcel.gemarkung} · Flur ${d.parcel.flur} · Flurstück ${d.parcel.nummer}</b><br>${d.parcel.kreis}<br>Fläche: ${d.parcel.flaeche?Math.round(d.parcel.flaeche).toLocaleString('de-DE')+' m²':'--'}${d.parcel.lage?'<br>Lage: '+d.parcel.lage:''}<br><span class="muted">ALKIS: ${d.parcel.aktualit||'--'}</span>`;await loadTargets();drawParcel();}
async function loadTargets(){if(!selectedParcel)return;targets=await fetch(`/api/parcel/${selectedParcel.id}/targets?spacing=${spacing.value}`).then(r=>r.json());targetlist.innerHTML='';targets.forEach((t,i)=>{t._idx=i;let d=document.createElement('div');d.className='target';d.innerHTML=`<button>${t.name} <span class="muted">${t.kind==='helper'?'Hilfspunkt':'ALKIS-Eckpunkt'}</span></button><button class="go">GO</button>`;d.children[0].onclick=()=>showTargetOnMap(t);d.children[1].onclick=()=>setTarget(t);targetlist.appendChild(d)});drawTargetPoints()}
function setTarget(t){target=t;targetname.textContent=`Ziel: ${t.name}`;if(map){if(targetMarker)targetMarker.remove();targetMarker=new maplibregl.Marker({color:'#e53935'}).setLngLat([t.lon,t.lat]).addTo(map);map.flyTo({center:[t.lon,t.lat],zoom:19})}document.querySelector('.tabs button').click()}
async function nearestCurrentBoundary(){if(!selectedParcel){alert('Bitte zuerst ein Flurstück wählen oder das erkannte Flurstück laden.');return}let t=await fetch(`/api/nearest-boundary/${selectedParcel.id}`).then(r=>r.json());setTarget({name:'Nächster Grenzpunkt',E:t.E,N:t.N,lon:t.lon,lat:t.lat,kind:'projection'})}
function clearTargetLabels(){targetLabelMarkers.forEach(m=>m.remove());targetLabelMarkers=[]}
function targetFeatureCollection(){return {type:'FeatureCollection',features:targets.map((t,i)=>({type:'Feature',properties:{idx:i,name:t.name,kind:t.kind},geometry:{type:'Point',coordinates:[t.lon,t.lat]}}))}}
function drawTargetPoints(){
 if(!map||!map.isStyleLoaded())return;
 let fc=targetFeatureCollection();
 if(map.getSource('target-points'))map.getSource('target-points').setData(fc);else{
  map.addSource('target-points',{type:'geojson',data:fc});
  map.addLayer({id:'target-circles',type:'circle',source:'target-points',paint:{'circle-radius':['case',['==',['get','kind'],'helper'],4,6],'circle-color':['case',['==',['get','kind'],'helper'],'#43a5ff','#ff9800'],'circle-stroke-color':'#111','circle-stroke-width':1.5}});
 }
 refreshTargetLabels();
}
function refreshTargetLabels(){
 clearTargetLabels();if(!map||map.getZoom()<16||!targets.length)return;
 let b=map.getBounds(),visible=targets.filter(t=>b.contains([t.lon,t.lat]));
 if(visible.length>300)visible=visible.filter(t=>t.kind!=='helper').concat(visible.filter(t=>t.kind==='helper').slice(0,Math.max(0,300-visible.filter(t=>t.kind!=='helper').length)));
 visible.slice(0,300).forEach(t=>{let el=document.createElement('div');el.className='pointlabel '+(t.kind==='helper'?'helper':'');el.textContent=t.name;el.onclick=(ev)=>{ev.stopPropagation();showTargetPopup(t)};targetLabelMarkers.push(new maplibregl.Marker({element:el,anchor:'bottom'}).setLngLat([t.lon,t.lat]).addTo(map))});
}
function showTargetPopup(t){
 if(!map)return;let type=t.kind==='helper'?'Hilfspunkt':'ALKIS-Eckpunkt';
 new maplibregl.Popup({offset:12}).setLngLat([t.lon,t.lat]).setHTML(`<b>${t.name}</b><br>${type}<br>E ${t.E.toFixed(3)}<br>N ${t.N.toFixed(3)}<br><button style="margin-top:7px" onclick="goTargetByIndex(${t._idx})">Diesen Punkt ansteuern</button>`).addTo(map)
}
function goTargetByIndex(i){if(targets[i])setTarget(targets[i])}
function showTargetOnMap(t){tab('mapPane',document.querySelectorAll('.tabs button')[2]);setTimeout(()=>{if(map){map.flyTo({center:[t.lon,t.lat],zoom:20});showTargetPopup(t)}},200)}

async function initMap(){
 if(map)return;
 if(typeof maplibregl==='undefined'){mapdiag.textContent='FEHLER: MapLibre wurde nicht geladen.';mapdiag.className='card danger';return}
 let mi=await fetch('/api/map/info').then(r=>r.json()).catch(e=>({exists:false,error:String(e)}));
 if(!mi.exists){mapdiag.textContent='Offlinekarte fehlt. Bitte MAP_REPAIR.sh ausführen.';mapdiag.className='card danger'}
 else if(mi.error){mapdiag.textContent='Offlinekarte kann nicht gelesen werden: '+mi.error;mapdiag.className='card danger'}
 else {mapdiag.textContent=`Offlinekarte OK · ${mi.size_mb} MB · ${mi.tiles.toLocaleString('de-DE')} Kacheln · Zoom ${mi.minzoom}–${mi.maxzoom}`;mapdiag.className='card muted'}
 map=new maplibregl.Map({container:'map',style:'/api/map/style',center:[11.7,51.9],zoom:8,maxZoom:21});
 map.addControl(new maplibregl.NavigationControl());map.addControl(new maplibregl.ScaleControl({unit:'metric'}));
 map.on('load',()=>{drawParcel();drawTargetPoints();poll()});
 map.on('error',e=>{if(e&&e.error)mapdiag.textContent='Kartenfehler: '+e.error.message});
 map.on('click','near-lines',e=>{if(e.features&&e.features.length)loadParcelById(parseInt(e.features[0].properties.id))});
 map.on('mouseenter','near-lines',()=>map.getCanvas().style.cursor='pointer');map.on('mouseleave','near-lines',()=>map.getCanvas().style.cursor='');map.on('moveend',refreshTargetLabels);map.on('zoomend',refreshTargetLabels);map.on('click','target-circles',e=>{if(e.features&&e.features.length){let i=parseInt(e.features[0].properties.idx);if(targets[i])showTargetPopup(targets[i])}});map.on('mouseenter','target-circles',()=>map.getCanvas().style.cursor='pointer');map.on('mouseleave','target-circles',()=>map.getCanvas().style.cursor='')
}

async function refreshNearby(E,N){
 if(!map||!map.isStyleLoaded())return;lastNearE=E;lastNearN=N;
 let a=await fetch(`/api/parcels/near?E=${E}&N=${N}&r=140`).then(r=>r.json()).catch(()=>[]);
 let fc={type:'FeatureCollection',features:a.map(p=>({type:'Feature',properties:{id:p.id,nummer:p.nummer,gemarkung:p.gemarkung,flur:p.flur},geometry:p.geojson}))};
 if(map.getSource('near'))map.getSource('near').setData(fc);else{map.addSource('near',{type:'geojson',data:fc});map.addLayer({id:'near-lines',type:'line',source:'near',paint:{'line-color':'#606060','line-width':['interpolate',['linear'],['zoom'],15,1,20,2]}})}
}

function drawParcel(){if(!map||!parcelFeature||!map.isStyleLoaded())return;if(map.getSource('parcel')){map.getSource('parcel').setData(parcelFeature)}else{map.addSource('parcel',{type:'geojson',data:parcelFeature});map.addLayer({id:'parcel-fill',type:'fill',source:'parcel',paint:{'fill-color':'#ffcc00','fill-opacity':0.18}});map.addLayer({id:'parcel-line',type:'line',source:'parcel',paint:{'line-color':'#ff9800','line-width':4}})}let coords=[];function walk(a){if(typeof a[0]==='number')coords.push(a);else a.forEach(walk)}walk(parcelFeature.geometry.coordinates);if(coords.length){let b=coords.reduce((bb,p)=>bb.extend(p),new maplibregl.LngLatBounds(coords[0],coords[0]));map.fitBounds(b,{padding:45,maxZoom:19})}}

function fmtDelta(m){if(m==null)return'--';let mm=Math.round(m*1000);return (mm>0?'+':'')+mm+' mm'}
function heightClass(level){return level==='green'?'heightgreen':(level==='yellow'?'heightyellow':(level==='red'?'heightred':'heightneutral'))}
function trafficText(delta,level){if(delta==null)return'Noch kein Sollvergleich';let mm=Math.round(delta*1000);let word=level==='green'?'GRÜN':(level==='yellow'?'GELB':'ROT');return `${word} · ${mm>0?'+':''}${mm} mm`}
function updateLiveTraffic(msl,agl){
 if(!heightData||!heightData.settings){htraffic.textContent='Noch kein Sollvergleich';htraffic.className='traffic heightneutral';return}
 let st=heightData.settings,delta=null;if(st.mode==='MSL'&&msl!=null&&st.target_msl_m!=null)delta=msl-st.target_msl_m;if(st.mode==='AGL'&&agl!=null&&st.target_agl_m!=null)delta=agl-st.target_agl_m;
 let tol=(st.tolerance_mm||20)/1000,level=delta==null?null:(Math.abs(delta)<=tol?'green':(Math.abs(delta)<=2*tol?'yellow':'red'));
 htraffic.textContent=trafficText(delta,level);htraffic.className='traffic '+heightClass(level)
}
async function loadHeights(){
 let d=await fetch('/api/height/points').then(r=>r.json());heightData=d;let st=d.settings||{};
 hmode.value=st.mode||'AGL';htol.value=st.tolerance_mm==null?20:st.tolerance_mm;hmsltarget.value=st.target_msl_m==null?'':Number(st.target_msl_m).toFixed(3);hagltarget.value=st.target_agl_m==null?'0.000':Number(st.target_agl_m).toFixed(3);
 heightlist.innerHTML='';
 if(!d.points||!d.points.length){heightlist.textContent='Noch keine Punkte.';return}
 d.points.forEach(p=>{
  let row=document.createElement('div');row.className='heightrow'+(p.is_reference?' ref':'');let agl=p.agl_m==null?'--':((p.agl_m>=0?'+':'')+p.agl_m.toFixed(3)+' m');
  row.innerHTML=`<div><b>${p.name}</b>${p.is_reference?' <span class="warn">REF</span>':''}<br><span class="muted">MSL ${p.height_m.toFixed(3)} m · AGL ${agl}<br>σ ${(p.sigma_m*1000).toFixed(1)} mm · Spannw. ${(p.spread_m*1000).toFixed(1)} mm · ${p.samples} Werte</span></div><div class="heightdelta ${heightClass(p.level)}">${fmtDelta(p.active_delta_m)}</div><div><button class="smallbtn" onclick="setHeightRef(${p.id})">REF</button><button class="smallbtn" onclick="deleteHeight(${p.id})">✕</button></div>`;
  heightlist.appendChild(row)
 });
 let next=d.points.length+1;if(hname.value.match(/^Punkt \d+$/))hname.value='Punkt '+next
}
async function saveHeightSettings(){
 let payload={mode:hmode.value,target_msl_m:hmsltarget.value===''?null:parseFloat(hmsltarget.value),target_agl_m:hagltarget.value===''?0:parseFloat(hagltarget.value),tolerance_mm:parseFloat(htol.value||'20')};
 let r=await fetch('/api/height/settings',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});heightData=await r.json();hmsg.textContent='Sollhöhe gespeichert.';await loadHeights()
}
async function measureHeight(){
 hmeasure.disabled=true;hmeasure.classList.add('measuring');hmeasure.textContent='Messung läuft…';hmsg.textContent='Antenne ruhig und lotrecht halten.';
 let payload={name:hname.value,pole_height:parseFloat(hpole.value),duration:parseFloat(hduration.value)};
 try{let r=await fetch('/api/height/capture',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});let d=await r.json();if(!r.ok)throw new Error(d.error||'Messfehler');hmsg.textContent=`Gespeichert: ${d.point.name} · σ ${(d.point.sigma_m*1000).toFixed(1)} mm · ${d.point.samples} Werte`;await loadHeights()}catch(e){hmsg.textContent=e.message}
 hmeasure.disabled=false;hmeasure.classList.remove('measuring');hmeasure.textContent='Punkt messen'
}
async function setHeightRef(id){await fetch('/api/height/reference/'+id,{method:'POST'});loadHeights()}
async function deleteHeight(id){await fetch('/api/height/point/'+id,{method:'DELETE'});loadHeights()}
async function clearHeights(){if(confirm('Alle gespeicherten Höhenpunkte löschen?')){await fetch('/api/height/clear',{method:'POST'});loadHeights()}}
loadHeights();loadCounties();
</script></body></html>'''

@app.route('/')
def index():return render_template_string(HTML)

if __name__=='__main__':
    threading.Thread(target=serial_worker,daemon=True).start();threading.Thread(target=ntrip_worker,daemon=True).start();threading.Thread(target=network_worker,daemon=True).start()
    app.run(host='0.0.0.0',port=5000,debug=False,threaded=True)
