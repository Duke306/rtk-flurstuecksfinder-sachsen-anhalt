"""Network probes must leave GNSS state available while they wait."""
import threading
import unittest
from unittest.mock import patch

import app


class NetworkStatusTests(unittest.TestCase):
    def test_probes_allow_gnss_updates_and_publish_status_together(self):
        lock = threading.Lock()
        state = dict(wifi_ssid='old', local_ip='old', internet=False, alt=100)

        def probe(result):
            def run():
                self.assertTrue(lock.acquire(blocking=False),
                                'Network probe blocks GNSS state updates')
                try:
                    self.assertEqual((state['wifi_ssid'], state['local_ip'], state['internet']),
                                     ('old', 'old', False))
                    state['alt'] += 1  # Simulate a receiver update during each probe.
                finally:
                    lock.release()
                return result
            return run

        class EndIteration(Exception):
            pass

        with patch.object(app, 'state_lock', lock), patch.object(app, 'state', state), \
                patch.object(app, 'get_wifi_ssid', side_effect=probe('rover')), \
                patch.object(app, 'get_local_ip', side_effect=probe('192.0.2.1')), \
                patch.object(app, 'internet_ok', side_effect=probe(True)), \
                patch.object(app.time, 'sleep', side_effect=EndIteration) as sleep:
            with self.assertRaises(EndIteration):
                app.network_worker()
        self.assertEqual(state, dict(wifi_ssid='rover', local_ip='192.0.2.1',
                                     internet=True, alt=103))
        sleep.assert_called_once_with(3)
        self.assertFalse(lock.locked())
