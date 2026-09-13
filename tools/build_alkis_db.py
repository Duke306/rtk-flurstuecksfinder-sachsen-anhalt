#!/usr/bin/env python3
"""Build a compact offline ALKIS parcel DB from Sachsen-Anhalt GBIS_Flurstuecke_*.zip files.
Uses only Python standard library. Geometry stays at millimetre resolution in EPSG:25832.
"""
import argparse, sqlite3, zipfile, xml.etree.ElementTree as ET, json, zlib, time, os, glob, sys

def local(tag): return tag.rsplit('}',1)[-1]
def txt(el,name,default=''):
    c=el.find('./{*}'+name);return (c.text or '').strip() if c is not None else default
def numi(v,d=0):
    try:return int(v)
    except:return d
def numf(v,d=None):
    try:return float(v)
    except:return d

def ring(pos):
    vals=[float(x) for x in (pos.text or '').split()]
    return [[vals[i],vals[i+1]] for i in range(0,len(vals)-1,2)]
def geometry(el):
    polys=[]
    for p in el.findall('.//{http://www.opengis.net/gml/3.2}Polygon'):
        ext=p.find('./{http://www.opengis.net/gml/3.2}exterior/{http://www.opengis.net/gml/3.2}LinearRing/{http://www.opengis.net/gml/3.2}posList')
        if ext is None:continue
        rr=[ring(ext)]
        for inn in p.findall('./{http://www.opengis.net/gml/3.2}interior/{http://www.opengis.net/gml/3.2}LinearRing/{http://www.opengis.net/gml/3.2}posList'):rr.append(ring(inn))
        polys.append(rr)
    return polys
def bounds(polys):
    minx=miny=float('inf');maxx=maxy=float('-inf')
    for p in polys:
        for r in p:
            for x,y in r:minx=min(minx,x);maxx=max(maxx,x);miny=min(miny,y);maxy=max(maxy,y)
    return minx,maxx,miny,maxy
def encode(polys):
    minx,maxx,miny,maxy=bounds(polys);x0=round(minx*1000);y0=round(miny*1000)
    pp=[]
    for p in polys:
        pp.append([[[round(x*1000)-x0,round(y*1000)-y0] for x,y in r] for r in p])
    raw=json.dumps({'x':x0,'y':y0,'p':pp},separators=(',',':')).encode()
    return zlib.compress(raw,9),(minx,maxx,miny,maxy)

def create_db(path):
    if os.path.exists(path):os.remove(path)
    db=sqlite3.connect(path);db.executescript('''
    PRAGMA journal_mode=OFF; PRAGMA synchronous=OFF; PRAGMA temp_store=MEMORY; PRAGMA cache_size=-200000;
    CREATE TABLE parcels(id INTEGER PRIMARY KEY,flstkennz TEXT UNIQUE,kreis TEXT,kreisschl TEXT,gemeinde TEXT,gmdschl TEXT,gemarkung TEXT,gemaschl TEXT,flur INTEGER,zaehler INTEGER,nenner INTEGER,flaeche REAL,lage TEXT,nutzung TEXT,aktualit TEXT,geom BLOB,minx REAL,maxx REAL,miny REAL,maxy REAL);
    CREATE VIRTUAL TABLE parcel_rtree USING rtree(id,minx,maxx,miny,maxy);
    CREATE TABLE sources(filename TEXT PRIMARY KEY,feature_count INTEGER,imported_at TEXT);
    CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT);
    ''');return db

def import_zip(db,path):
    name=os.path.basename(path);z=zipfile.ZipFile(path);xmls=[n for n in z.namelist() if n.lower().endswith(('.xml','.gml'))]
    if not xmls:raise RuntimeError(f'{name}: keine XML/GML-Datei')
    print(f'Importiere {name} ...',flush=True);count=0;t=time.time();c=db.cursor()
    for xmlname in xmls:
        print(f'  lese {xmlname}',flush=True)
        with z.open(xmlname) as f:
            context=ET.iterparse(f,events=('start','end'))
            _,root=next(context)
            for event,el in context:
                if event!='end' or local(el.tag)!='Flurstueck':continue
                try:
                    polys=geometry(el)
                    if not polys:continue
                    blob,(minx,maxx,miny,maxy)=encode(polys)
                    row=(txt(el,'flstkennz'),txt(el,'kreis'),txt(el,'kreisschl'),txt(el,'gemeinde'),txt(el,'gmdschl'),txt(el,'gemarkung'),txt(el,'gemaschl'),numi(txt(el,'flur')),numi(txt(el,'flstnrzae')),numi(txt(el,'flstnrnen')),numf(txt(el,'flaeche')),txt(el,'lagebeztxt'),txt(el,'tntxt'),txt(el,'aktualit'),sqlite3.Binary(blob),minx,maxx,miny,maxy)
                    c.execute('INSERT INTO parcels(flstkennz,kreis,kreisschl,gemeinde,gmdschl,gemarkung,gemaschl,flur,zaehler,nenner,flaeche,lage,nutzung,aktualit,geom,minx,maxx,miny,maxy) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',row)
                    pid=c.lastrowid;c.execute('INSERT INTO parcel_rtree VALUES(?,?,?,?,?)',(pid,minx,maxx,miny,maxy));count+=1
                    if count%10000==0:db.commit();print(f'  {count:,} Flurstücke',flush=True)
                except sqlite3.IntegrityError:
                    pass
                finally:
                    el.clear()
                    root.clear()
    db.commit();c.execute('INSERT OR REPLACE INTO sources VALUES(?,?,datetime("now"))',(name,count));db.commit()
    print(f'  fertig: {count:,} in {time.time()-t:.1f}s',flush=True);return count

def main():
    ap=argparse.ArgumentParser();ap.add_argument('input',help='Ordner mit GBIS_Flurstuecke_*.zip oder einzelne ZIP');ap.add_argument('output',help='Ausgabe .sqlite')
    a=ap.parse_args();paths=[a.input] if os.path.isfile(a.input) else sorted(glob.glob(os.path.join(a.input,'*.zip')))
    paths=[p for p in paths if 'flurstueck' in os.path.basename(p).lower()]
    if not paths:sys.exit('Keine Flurstück-ZIPs gefunden.')
    db=create_db(a.output);total=0
    for p in paths:
        try:total+=import_zip(db,p)
        except Exception as e:print('FEHLER',p,e,file=sys.stderr)
    print('Erzeuge Suchindizes ...',flush=True)
    db.executescript('''CREATE INDEX idx_lookup ON parcels(gemaschl,flur,zaehler,nenner);CREATE INDEX idx_gemarkung ON parcels(gemarkung COLLATE NOCASE);CREATE INDEX idx_kreis ON parcels(kreisschl,kreis);ANALYZE;PRAGMA optimize;''')
    db.execute('INSERT OR REPLACE INTO metadata VALUES("crs","EPSG:25832")');db.execute('INSERT OR REPLACE INTO metadata VALUES("parcel_count",?)',(str(total),));db.commit();db.close()
    print(f'FERTIG: {total:,} Flurstücke, {os.path.getsize(a.output)/1024/1024:.1f} MB -> {a.output}')
if __name__=='__main__':main()
