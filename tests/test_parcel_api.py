"""Tests for issue #1: /api/parcel/<pid> must load a parcel with a single
DB query instead of two, while keeping the existing response shape:
- 'parcel' keeps the same fields as the old get(pid, False) (no 'nutzung').
- 'feature.properties' keeps the same fields as the old parcel_geojson()
  (includes 'nutzung').
- A missing parcel still yields 404 with {'error': 'nicht gefunden'}.
"""
import json
import os
import sqlite3
import sys
import tempfile
import unittest
import zlib

BASE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(BASE)
sys.path.insert(0, REPO_ROOT)


def encode_geom(polys):
    """Inverse of ParcelDB.decode_geom: polys is [[[ [x,y], ... ring ] poly] ...]
    using real (already-projected) coordinates; x0/y0 offset kept at 0 for
    simplicity so stored ints are just coordinate*1000 (millimetres)."""
    p = [[[[round(x * 1000), round(y * 1000)] for x, y in ring] for ring in poly] for poly in polys]
    return zlib.compress(json.dumps({'x': 0, 'y': 0, 'p': p}).encode('utf-8'))


SQUARE = [[[[671700.0, 5738300.0], [671710.0, 5738300.0], [671710.0, 5738310.0],
            [671700.0, 5738310.0], [671700.0, 5738300.0]]]]


def build_test_db(path):
    conn = sqlite3.connect(path)
    conn.execute('''CREATE TABLE parcels(id INTEGER PRIMARY KEY,flstkennz TEXT UNIQUE,kreis TEXT,
        kreisschl TEXT,gemeinde TEXT,gmdschl TEXT,gemarkung TEXT,gemaschl TEXT,flur INTEGER,
        zaehler INTEGER,nenner INTEGER,flaeche REAL,lage TEXT,nutzung TEXT,aktualit TEXT,geom BLOB,
        minx REAL,maxx REAL,miny REAL,maxy REAL)''')
    conn.execute('''INSERT INTO parcels(id,flstkennz,kreis,kreisschl,gemeinde,gmdschl,gemarkung,
        gemaschl,flur,zaehler,nenner,flaeche,lage,nutzung,aktualit,geom,minx,maxx,miny,maxy)
        VALUES (1,'FK001','Testkreis','15001','Testgemeinde','150010001','Testgemarkung',
        '1500100001',1,42,NULL,100.0,'Musterweg 1','Landwirtschaft','2024-01-01',?,
        671700.0,671710.0,5738300.0,5738310.0)''', (encode_geom(SQUARE),))
    conn.execute('''CREATE VIRTUAL TABLE parcel_rtree USING rtree(id,minx,maxx,miny,maxy)''')
    conn.execute('INSERT INTO parcel_rtree(id,minx,maxx,miny,maxy) VALUES (1,671700.0,671710.0,5738300.0,5738310.0)')
    conn.commit()
    conn.close()


class ParcelDBUnitTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmpdir.name, 'alkis_test.sqlite')
        build_test_db(self.db_path)
        from parcel_db import ParcelDB
        self.db = ParcelDB(self.db_path)

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_parcel_and_geojson_matches_old_two_call_result(self):
        old_summary = self.db.get(1, False)
        old_feature = self.db.parcel_geojson(1)

        new_summary, new_feature = self.db.parcel_and_geojson(1)

        self.assertEqual(new_summary, old_summary)
        self.assertEqual(new_feature, old_feature)
        # sanity: the fields actually differ from each other (nutzung only
        # appears on the feature side) so the test would catch a merge bug.
        self.assertNotIn('nutzung', new_summary)
        self.assertIn('nutzung', new_feature['properties'])

    def test_parcel_and_geojson_missing_pid(self):
        summary, feature = self.db.parcel_and_geojson(9999)
        self.assertIsNone(summary)
        self.assertIsNone(feature)

    def test_parcel_and_geojson_issues_single_query(self):
        conn = self.db.conn()
        calls = []
        conn.set_trace_callback(calls.append)
        try:
            self.db.parcel_and_geojson(1)
        finally:
            conn.set_trace_callback(None)
        select_calls = [c for c in calls if c.strip().upper().startswith('SELECT')]
        self.assertEqual(len(select_calls), 1, f'expected exactly one SELECT, got {select_calls}')


class ParcelRouteTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmpdir = tempfile.TemporaryDirectory()
        cls.db_path = os.path.join(cls.tmpdir.name, 'alkis_test.sqlite')
        build_test_db(cls.db_path)
        os.environ['RTK_PARCEL_DB'] = cls.db_path
        os.environ['RTK_MAP_DB'] = os.path.join(cls.tmpdir.name, 'nonexistent.mbtiles')
        os.environ['RTK_HEIGHT_FILE'] = os.path.join(cls.tmpdir.name, 'height_points.json')
        sys.modules.pop('app', None)
        import app as app_module
        cls.app_module = app_module
        cls.client = app_module.app.test_client()

    @classmethod
    def tearDownClass(cls):
        cls.tmpdir.cleanup()
        os.environ.pop('RTK_PARCEL_DB', None)
        os.environ.pop('RTK_MAP_DB', None)
        os.environ.pop('RTK_HEIGHT_FILE', None)
        sys.modules.pop('app', None)

    def test_existing_parcel_response_shape_unchanged(self):
        resp = self.client.get('/api/parcel/1')
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertIn('parcel', data)
        self.assertIn('feature', data)
        self.assertNotIn('nutzung', data['parcel'])
        self.assertIn('nutzung', data['feature']['properties'])
        self.assertEqual(data['parcel']['nummer'], '42')
        self.assertEqual(data['feature']['type'], 'Feature')
        self.assertEqual(data['feature']['geometry']['type'], 'Polygon')

    def test_missing_parcel_returns_404(self):
        resp = self.client.get('/api/parcel/9999')
        self.assertEqual(resp.status_code, 404)
        self.assertEqual(resp.get_json(), {'error': 'nicht gefunden'})


if __name__ == '__main__':
    unittest.main()
