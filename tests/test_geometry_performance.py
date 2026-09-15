"""Check cache bounds, geometric equivalence and unchanged map/API behavior."""
import copy
import json
import tempfile
import unittest
import zlib
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from parcel_db import ParcelDB, _GeometryCache
from test_parcel_api import build_test_db


def blob(offset=0):
    return zlib.compress(json.dumps({'x': 671700000 + offset, 'y': 5738300000,
        'p': [[[[0, 0], [10000, 0], [10000, 10000], [0, 0]]]]}).encode())


class GeometryTests(unittest.TestCase):
    def test_reuses_decode_with_immutable_values(self):
        cache = _GeometryCache()
        with patch('parcel_db.zlib.decompress', wraps=zlib.decompress) as decode:
            a = cache.decode(blob())
            self.assertIs(a, cache.decode(blob()))
            self.assertEqual(decode.call_count, 1)
        self.assertEqual(a[0][0][1], (671710.0, 5738300.0))
        with self.assertRaises(TypeError):
            a[0][0][0][0] = 1

    def test_entry_limit_and_lru_order(self):
        cache = _GeometryCache(max_entries=2)
        first = cache.decode(blob(1))
        cache.decode(blob(2))
        cache.decode(blob(1))
        cache.decode(blob(3))
        self.assertEqual(len(cache.entries), 2)
        self.assertNotIn(blob(2), cache.entries)
        self.assertIs(first, cache.decode(blob(1)))

    def test_byte_budget_evicts_and_oversized_values_are_not_retained(self):
        probe = _GeometryCache()
        probe.decode(blob())
        budget = probe.retained_bytes
        cache = _GeometryCache(max_bytes=budget + 20)
        for i in range(5):
            cache.decode(blob(i))
            self.assertLessEqual(cache.retained_bytes, budget + 20)
        self.assertEqual(len(cache.entries), 1)
        tiny = _GeometryCache(max_bytes=1)
        self.assertEqual(tiny.decode(blob())[0][0][0], (671700., 5738300.))
        self.assertEqual(tiny.retained_bytes, 0)
        self.assertEqual(len(tiny.entries), 0)

    def test_threaded_reads_and_failed_decode(self):
        cache = _GeometryCache()
        with ThreadPoolExecutor(max_workers=4) as pool:
            values = list(pool.map(cache.decode, [blob()] * 20))
        self.assertTrue(all(x is values[0] for x in values))
        with self.assertRaises(zlib.error):
            cache.decode(b'invalid')
        self.assertEqual(len(cache.entries), 1)
        self.assertIs(cache.decode(blob()), values[0])

    def test_batched_geometry_matches_scalar_with_holes_and_empty_rings(self):
        db = ParcelDB(':memory:')
        outer = [[671700, 5738300], [671710, 5738300], [671710, 5738310], [671700, 5738300]]
        hole = [[671704, 5738302], [671706, 5738302], [671705, 5738304], [671704, 5738302]]
        for polys in ([], [[outer, hole]], [[outer, hole], [outer]], [[[]]]):
            coords = [[[list(db.to_geo.transform(*xy)) for xy in ring] for ring in poly] for poly in polys]
            expected = {'type': 'Polygon', 'coordinates': coords[0]} if len(coords) == 1 else {'type': 'MultiPolygon', 'coordinates': coords}
            self.assertEqual(db.geom_geojson(polys), expected)

    def test_cached_geometry_still_detects_sub_30cm_boundary_crossing(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / 'test.sqlite')
            build_test_db(path)
            db = ParcelDB(path)
            try:
                self.assertEqual([p['id'] for p in db.at_point(671709.95, 5738305)], [1])
                self.assertEqual(db.at_point(671710.05, 5738305), [])
                before = db.get(1, True)['polys']
                db.targets(1, 2)
                db.near(671705, 5738305)
                self.assertIs(db.get(1, True)['polys'], before)
            finally:
                db.conn().close()

    def test_map_style_does_not_mutate_template_across_hosts(self):
        import app
        original = copy.deepcopy(app.MAP_STYLE)
        client = app.app.test_client()
        for host in ('http://first.local', 'http://second.local'):
            response = client.get('/api/map/style', base_url=host)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json['sources']['osm']['tiles'], [host + '/tiles/{z}/{x}/{y}.pbf'])
            self.assertEqual(app.MAP_STYLE, original)

    def test_targets_route_batches_and_preserves_empty_results(self):
        import app
        from unittest.mock import Mock
        targets = [{'name': 'P1', 'E': 671700., 'N': 5738300., 'kind': 'vertex'},
                   {'name': 'Z1', 'E': 671710., 'N': 5738310., 'kind': 'helper'}]
        expected = []
        for t in targets:
            lon, lat = app.utm_to_geo.transform(t['E'], t['N'])
            expected.append(dict(t, lon=lon, lat=lat))
        parcels = Mock()
        transformer = Mock(wraps=app.utm_to_geo)
        with patch.object(app, 'db_required', return_value=True), patch.object(app, 'parcels', parcels), patch.object(app, 'utm_to_geo', transformer):
            parcels.targets.return_value = copy.deepcopy(targets)
            self.assertEqual(app.app.test_client().get('/api/parcel/1/targets').json, expected)
            self.assertEqual(transformer.transform.call_count, 1)
            parcels.targets.return_value = []
            self.assertEqual(app.app.test_client().get('/api/parcel/99/targets').json, [])
            self.assertEqual(transformer.transform.call_count, 1)
