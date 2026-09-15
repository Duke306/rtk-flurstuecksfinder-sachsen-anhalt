"""Height-v6 API regressions; no receiver or real measurements required."""
import importlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


class HeightV6Tests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / 'nested' / 'height_points.json'
        self.env = patch.dict(os.environ, {'RTK_HEIGHT_FILE': str(self.path),
                                         'RTK_PARCEL_DB': str(self.path.parent / 'none.sqlite')})
        self.env.start()
        self.previous = sys.modules.pop('app', None)
        self.mod = importlib.import_module('app')
        self.client = self.mod.app.test_client()

    def tearDown(self):
        sys.modules.pop('app', None)
        if self.previous is not None:
            sys.modules['app'] = self.previous
        self.env.stop()
        self.tmp.cleanup()

    def capture(self, role='point', alt=102.0, pole=2.0, quality=4, during=None):
        m = self.mod
        now = [1000.0]
        m.state.update(fix='RTK FIX', alt=alt, serial_connected=True, last_fix_time=1000.0)
        m.height_samples.clear()
        def sleep(seconds):
            now[0] += seconds
            if during:
                during()
            m.height_samples.append({'t': now[0], 'quality': quality, 'alt': alt,
                                     'E': 671700.0, 'N': 5738300.0})
        with patch.object(m.time, 'time', side_effect=lambda: now[0]), patch.object(m.time, 'sleep', side_effect=sleep):
            return self.client.post('/api/height/capture', json={'role': role, 'duration': 3, 'pole_height': pole})

    def test_explicit_reference_and_numbering(self):
        ref = self.capture('reference').get_json()['point']
        self.assertEqual(ref['display_code'], 'REF')
        first = self.capture(alt=102.3).get_json()['point']
        second = self.capture(alt=102.4).get_json()['point']
        self.assertEqual([first['code'], second['code']], ['H1', 'H2'])
        self.assertAlmostEqual(first['agl_m'], .3)
        self.assertAlmostEqual(first['height_m'], 100.3)
        self.assertTrue(-180 <= first['lon'] <= 180)
        self.assertTrue(-90 <= first['lat'] <= 90)

    def test_first_normal_point_is_not_automatic_reference(self):
        d = self.capture().get_json()['data']
        self.assertIsNone(d['reference_id'])
        self.assertIsNone(d['points'][0]['agl_m'])

    def test_zero_pole_and_persistence(self):
        self.assertEqual(self.capture(pole=0).get_json()['point']['height_m'], 102)
        self.assertEqual(json.loads(self.path.read_text())['points'][0]['pole_height_m'], 0)
        # Reload the module to model restart without any process-local data.
        sys.modules.pop('app')
        restarted = importlib.import_module('app')
        d = restarted.app.test_client().get('/api/height/points').get_json()
        self.assertEqual(d['points'][0]['height_m'], 102)
        self.assertEqual(d['points'][0]['display_code'], 'H1')

    def test_v5_names_reference_and_values_survive(self):
        old = {'reference_id': 3, 'settings': {'mode': 'AGL'}, 'points': [
            {'id': 3, 'name': 'OK Terrasse', 'height_m': 100, 'E': 671700, 'N': 5738300,
             'sigma_m': .005, 'duration_s': 10, 'samples': 10}]}
        self.path.parent.mkdir()
        self.path.write_text(json.dumps(old))
        d = self.client.get('/api/height/points').get_json()
        self.assertEqual(d['points'][0]['description'], 'OK Terrasse')
        self.assertEqual(d['points'][0]['display_code'], 'REF')
        self.assertEqual(json.loads(self.path.read_text()), old)

    def test_quality_independent_of_target_tolerance(self):
        self.capture('reference')
        d = self.mod._height_store_load()
        d['points'].append(dict(d['points'][0], id=2, code='H1', height_m=100, sigma_m=.05))
        self.mod._height_store_save(d)
        p = self.client.get('/api/height/points').get_json()['points'][1]
        self.assertEqual(p['level'], 'green')
        self.assertEqual(p['quality_level'], 'red')

    def test_fix_and_freshness_gate_and_invalid_samples(self):
        self.assertEqual(self.client.post('/api/height/capture', json={}).status_code, 409)
        self.mod.state.update(fix='RTK FIX', alt=102, serial_connected=True, last_fix_time=0)
        self.assertEqual(self.client.post('/api/height/capture', json={}).status_code, 409)
        self.assertEqual(self.capture(quality=5).status_code, 409)
        self.assertFalse(self.path.exists())

    def test_duplicate_capture_rejected(self):
        with self.mod.height_capture_lock:
            self.assertEqual(self.client.post('/api/height/capture', json={}).status_code, 409)
        self.assertEqual(self.capture().status_code, 200)

    def test_edits_during_capture_are_preserved(self):
        changed = []
        def edit_once():
            if not changed:
                changed.append(True)
                self.client.post('/api/height/settings', json={'tolerance_mm': 50})
        self.capture(during=edit_once)
        self.assertEqual(self.mod._height_store_load()['settings']['tolerance_mm'], 50)

    def test_reference_description_delete_and_clear(self):
        self.capture()
        self.assertEqual(self.client.patch('/api/height/point/1', json={'description': 'Test'}).status_code, 200)
        self.client.post('/api/height/reference/1')
        self.assertEqual(self.client.get('/api/height/points').get_json()['reference']['description'], 'Test')
        self.assertEqual(self.client.post('/api/height/reference/999').status_code, 404)
        self.client.delete('/api/height/point/1')
        self.assertIsNone(self.client.get('/api/height/points').get_json()['reference_id'])
        self.capture()
        self.client.post('/api/height/clear')
        self.assertEqual(self.client.get('/api/height/points').get_json()['points'], [])


if __name__ == '__main__':
    unittest.main()
