import sqlite3, json, zlib, math, threading, sys
from collections import OrderedDict
from pyproj import Transformer

class _GeometryCache:
    """Process-wide LRU with immutable values and a retained-size budget.

    Accounting includes blob keys, nested tuples/floats and an entry allowance.
    This limits retained cache data, not transient JSON decoding or total RSS.
    """
    def __init__(self, max_entries=128, max_bytes=8 * 1024 * 1024):
        self.max_entries = max_entries
        self.max_bytes = max_bytes
        self.entries = OrderedDict()
        self.retained_bytes = 0
        self.lock = threading.Lock()

    @staticmethod
    def _size(value):
        return sys.getsizeof(value) + (sum(_GeometryCache._size(x) for x in value)
                                     if isinstance(value, tuple) else 0)

    def decode(self, blob):
        with self.lock:
            if blob in self.entries:
                self.entries.move_to_end(blob)
                return self.entries[blob][0]
            d = json.loads(zlib.decompress(blob))
            x0, y0 = d['x'] / 1000.0, d['y'] / 1000.0
            polys = tuple(tuple(tuple((x0 + xy[0] / 1000.0, y0 + xy[1] / 1000.0)
                                      for xy in ring) for ring in poly) for poly in d['p'])
            size = sys.getsizeof(blob) + self._size(polys) + 512
            if self.max_entries <= 0 or size > self.max_bytes:
                return polys
            while self.entries and (len(self.entries) >= self.max_entries
                                    or self.retained_bytes + size > self.max_bytes):
                _, (_, old_size) = self.entries.popitem(last=False)
                self.retained_bytes -= old_size
            self.entries[blob] = (polys, size)
            self.retained_bytes += size
            return polys


_geometry_cache = _GeometryCache()


class ParcelDB:
    def __init__(self, path):
        self.path = path
        self.local = threading.local()
        self.to_geo = Transformer.from_crs('EPSG:25832','EPSG:4326',always_xy=True)

    def conn(self):
        c=getattr(self.local,'conn',None)
        if c is None:
            c=sqlite3.connect(self.path, timeout=10)
            c.row_factory=sqlite3.Row
            c.execute('PRAGMA query_only=ON')
            c.execute('PRAGMA cache_size=-32768')
            self.local.conn=c
        return c

    @staticmethod
    def decode_geom(blob):
        return _geometry_cache.decode(blob)

    @staticmethod
    def _in_ring(x,y,ring):
        inside=False
        j=len(ring)-1
        for i,(xi,yi) in enumerate(ring):
            xj,yj=ring[j]
            if ((yi>y)!=(yj>y)) and (x < (xj-xi)*(y-yi)/(yj-yi)+xi):
                inside=not inside
            j=i
        return inside

    @classmethod
    def point_in_geom(cls,x,y,polys):
        for poly in polys:
            if poly and cls._in_ring(x,y,poly[0]) and not any(cls._in_ring(x,y,h) for h in poly[1:]):
                return True
        return False

    @staticmethod
    def _seg_dist(x,y,a,b):
        ax,ay=a; bx,by=b; dx=bx-ax; dy=by-ay
        if dx==0 and dy==0: return math.hypot(x-ax,y-ay),(ax,ay)
        t=((x-ax)*dx+(y-ay)*dy)/(dx*dx+dy*dy)
        t=max(0.0,min(1.0,t)); px=ax+t*dx; py=ay+t*dy
        return math.hypot(x-px,y-py),(px,py)

    @classmethod
    def nearest_boundary_point(cls,x,y,polys):
        best=(float('inf'),None)
        for poly in polys:
            for ring in poly:  # exteriors + interior holes are boundaries
                for a,b in zip(ring,ring[1:]):
                    d,p=cls._seg_dist(x,y,a,b)
                    if d<best[0]: best=(d,p)
        return best

    @staticmethod
    def _summary(r):
        if not r:return None
        d=dict(r)
        d['nummer']=str(d['zaehler']) + (('/'+str(d['nenner'])) if d['nenner'] else '')
        d.pop('geom',None)
        return d

    def get(self,pid,with_geom=False):
        cols='*' if with_geom else 'id,flstkennz,kreis,kreisschl,gemeinde,gmdschl,gemarkung,gemaschl,flur,zaehler,nenner,flaeche,lage,aktualit,minx,maxx,miny,maxy'
        r=self.conn().execute(f'SELECT {cols} FROM parcels WHERE id=?',(pid,)).fetchone()
        if not r:return None
        d=dict(r); d['nummer']=str(d['zaehler'])+(('/'+str(d['nenner'])) if d['nenner'] else '')
        if with_geom:d['polys']=self.decode_geom(d.pop('geom'))
        return d

    def at_point(self,x,y):
        rows=self.conn().execute('''SELECT p.* FROM parcel_rtree r JOIN parcels p ON p.id=r.id
          WHERE r.minx<=? AND r.maxx>=? AND r.miny<=? AND r.maxy>=?''',(x,x,y,y)).fetchall()
        out=[]
        for r in rows:
            polys=self.decode_geom(r['geom'])
            if self.point_in_geom(x,y,polys):
                d=self._summary(r); out.append(d)
        return out

    def near(self,x,y,radius=100,limit=150):
        rows=self.conn().execute('''SELECT p.id,p.flstkennz,p.kreis,p.gemarkung,p.gemaschl,p.flur,p.zaehler,p.nenner,p.flaeche,p.lage,p.geom
          FROM parcel_rtree r JOIN parcels p ON p.id=r.id
          WHERE r.maxx>=? AND r.minx<=? AND r.maxy>=? AND r.miny<=? LIMIT ?''',
          (x-radius,x+radius,y-radius,y+radius,limit)).fetchall()
        result=[]
        for r in rows:
            polys=self.decode_geom(r['geom']); d=dict(r); d.pop('geom')
            d['nummer']=str(d['zaehler'])+(('/'+str(d['nenner'])) if d['nenner'] else '')
            d['geojson']=self.geom_geojson(polys)
            result.append(d)
        return result

    def counties(self):
        return [dict(r) for r in self.conn().execute('SELECT kreisschl,kreis,COUNT(*) n FROM parcels GROUP BY kreisschl,kreis ORDER BY kreis COLLATE NOCASE')]

    def gemarkungen(self,kreisschl=''):
        if kreisschl:
            q='SELECT gemaschl,gemarkung,kreis,COUNT(*) n FROM parcels WHERE kreisschl=? GROUP BY gemaschl,gemarkung,kreis ORDER BY gemarkung COLLATE NOCASE'
            args=(kreisschl,)
        else:
            q='SELECT gemaschl,gemarkung,kreis,COUNT(*) n FROM parcels GROUP BY gemaschl,gemarkung,kreis ORDER BY gemarkung COLLATE NOCASE'
            args=()
        return [dict(r) for r in self.conn().execute(q,args)]

    def fluren(self,gemaschl):
        return [r[0] for r in self.conn().execute('SELECT DISTINCT flur FROM parcels WHERE gemaschl=? ORDER BY flur',(gemaschl,))]

    def flurstuecke(self,gemaschl,flur):
        rows=self.conn().execute('SELECT id,zaehler,nenner,flaeche,lage FROM parcels WHERE gemaschl=? AND flur=? ORDER BY zaehler,nenner',(gemaschl,int(flur))).fetchall()
        return [{'id':r['id'],'zaehler':r['zaehler'],'nenner':r['nenner'],'nummer':str(r['zaehler'])+(('/'+str(r['nenner'])) if r['nenner'] else ''),'flaeche':r['flaeche'],'lage':r['lage']} for r in rows]

    def geom_geojson(self,polys):
        coords=[]
        for poly in polys:
            rr=[]
            for ring in poly:
                if not ring:
                    rr.append([])
                    continue
                xs, ys = zip(*ring)
                lons, lats = self.to_geo.transform(xs, ys)
                rr.append([list(xy) for xy in zip(lons, lats)])
            coords.append(rr)
        if len(coords)==1:return {'type':'Polygon','coordinates':coords[0]}
        return {'type':'MultiPolygon','coordinates':coords}

    def parcel_geojson(self,pid):
        p=self.get(pid,True)
        if not p:return None
        gj=self.geom_geojson(p['polys']); meta={k:v for k,v in p.items() if k!='polys'}
        return {'type':'Feature','properties':meta,'geometry':gj}

    SUMMARY_COLS=('id','flstkennz','kreis','kreisschl','gemeinde','gmdschl','gemarkung','gemaschl',
                  'flur','zaehler','nenner','flaeche','lage','aktualit','minx','maxx','miny','maxy','nummer')

    def parcel_and_geojson(self,pid):
        """Single-query equivalent of get(pid,False) + parcel_geojson(pid)."""
        p=self.get(pid,True)
        if not p:return None,None
        meta={k:v for k,v in p.items() if k!='polys'}
        summary={k:meta[k] for k in self.SUMMARY_COLS if k in meta}
        gj={'type':'Feature','properties':meta,'geometry':self.geom_geojson(p['polys'])}
        return summary,gj

    def targets(self,pid,spacing=0.0):
        p=self.get(pid,True)
        if not p:return []
        # Exterior rings only. Drop closing duplicate and dedupe identical coordinates across multi-surfaces.
        base=[];seen=set(); idx=1
        for poly in p['polys']:
            if not poly:continue
            ring=poly[0]
            pts=ring[:-1] if len(ring)>1 and ring[0]==ring[-1] else ring
            for x,y in pts:
                key=(round(x,3),round(y,3))
                if key in seen:continue
                seen.add(key); base.append({'name':f'P{idx}','E':x,'N':y,'kind':'vertex'});idx+=1
        if not spacing or spacing<=0:return base
        # Add equally spaced helper points on each exterior segment.
        helpers=[]; h=1
        for poly in p['polys']:
            if not poly:continue
            ring=poly[0]
            for a,b in zip(ring,ring[1:]):
                dx=b[0]-a[0];dy=b[1]-a[1];L=math.hypot(dx,dy)
                if L<=spacing:continue
                k=1
                while k*spacing<L-1e-6:
                    t=(k*spacing)/L; x=a[0]+t*dx;y=a[1]+t*dy
                    helpers.append({'name':f'Z{h}','E':x,'N':y,'kind':'helper'});h+=1;k+=1
                    if len(helpers)>=2000:break
                if len(helpers)>=2000:break
            if len(helpers)>=2000:break
        return base+helpers
