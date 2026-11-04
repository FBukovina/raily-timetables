import datetime as dt, json, pathlib, sys, tempfile, threading, unittest, urllib.request, http.server
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / 'scripts'))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import live_relay
from gtfs_realtime import decode
from test_gtfs_catalog import TIMES, feed

def varint(value):
    value &= (1 << 64) - 1; out = bytearray()
    while True:
        byte = value & 0x7F; value >>= 7
        out.append(byte | (0x80 if value else 0))
        if not value: return bytes(out)

def field(number, value):
    if isinstance(value, int): return varint(number << 3) + varint(value)
    data = value.encode() if isinstance(value, str) else value
    return varint(number << 3 | 2) + varint(len(data)) + data

def event(delay=None, time=None):
    return (field(1, delay) if delay is not None else b'') + (field(2, time) if time is not None else b'')

def stop(sequence, arrival=None, departure=None, relationship=None):
    out = field(1, sequence) + field(4, f'stop-{sequence}')
    if arrival is not None: out += field(2, arrival)
    if departure is not None: out += field(3, departure)
    if relationship is not None: out += field(5, relationship)
    return out

def trip(trip_id, date, stops=(), relationship=None, delay=None, timestamp=None):
    descriptor = field(1, trip_id) + field(3, date) + (field(4, relationship) if relationship is not None else b'')
    update = field(1, descriptor) + b''.join(field(2, s) for s in stops)
    if timestamp is not None: update += field(4, timestamp)
    if delay is not None: update += field(5, delay)
    return field(2, field(1, trip_id) + field(3, update))

def message(*entities, timestamp=1_793_800_000):
    return field(1, field(1, '2.0') + field(3, timestamp)) + b''.join(entities)

class LiveRelayTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(); root = pathlib.Path(cls.temp.name)
        cls.schedule = live_relay.Schedule([feed(root / 'feed.zip', TIMES, 'wd,2,20261104\nwd,1,20261107\n')])

    @classmethod
    def tearDownClass(cls): cls.temp.cleanup()

    def run_for(self, trip_id):
        return self.schedule.trips[trip_id][0]

    def test_decoder_reads_only_wanted_trips(self):
        data = message(trip('t1', '20261105', [stop(2, departure=event(60))]), trip('other', '20261105', [stop(1)]))
        header, updates = decode(data, wanted={'t1'}.__contains__)
        self.assertEqual(header, 1_793_800_000)
        self.assertEqual([u['trip_id'] for u in updates], ['t1'])
        self.assertEqual(updates[0]['stops'], [{'sequence': 2, 'stop_id': 'stop-2', 'departure': {'delay': 60}}])
        negative = decode(message(trip('t1', '20261105', [stop(2, arrival=event(-30))])))[1][0]
        self.assertEqual(negative['stops'][0]['arrival'], {'delay': -30})

    def test_updates_land_on_catalog_calls_and_propagate(self):
        variant = self.run_for('t1')
        # t1: Berlin Hbf 08:00, Spandau 08:15/08:16, Hamburg 09:45 on GTFS sequences 1, 2, 3.
        spandau = int(dt.datetime(2026, 11, 5, 8, 15, tzinfo=live_relay.ZONE).timestamp())
        data = message(trip('t1', '20261105', [stop(2, arrival=event(time=spandau + 120), departure=event(180))], timestamp=1_793_799_990),
                       trip('bus-or-unknown', '20261105', [stop(1, departure=event(60))]))
        header, runs = live_relay.runs_from_feed(self.schedule, data, 1_793_800_005)
        run = runs[f'delfi:{variant}:2026-11-05']
        self.assertEqual(list(runs), [f'delfi:{variant}:2026-11-05'])
        self.assertEqual(run['reportedAt'], 1_793_799_990)
        self.assertEqual(run['calls'], [{'sequence': 1, 'arrivalDelay': 120, 'departureDelay': 180},
                                        {'sequence': 2, 'propagated': True, 'arrivalDelay': 180}])

    def test_skipped_no_data_cancelled_and_trip_delay(self):
        variant = self.run_for('t1')
        data = message(trip('t1', '20261105', [stop(1, departure=event(60)), stop(2, relationship=1)]),
                       trip('t2', '20261105', relationship=3), trip('t4', '20261105', delay=240))
        _, runs = live_relay.runs_from_feed(self.schedule, data, 0)
        skipped = runs[f'delfi:{variant}:2026-11-05']['calls']
        self.assertEqual(skipped[0], {'sequence': 0, 'departureDelay': 60})
        self.assertEqual(skipped[1], {'sequence': 1, 'skipped': True})
        self.assertEqual(skipped[2], {'sequence': 2, 'propagated': True, 'arrivalDelay': 60})
        self.assertTrue(runs[f'delfi:{self.run_for("t2")}:2026-11-05']['cancelled'])
        night = runs[f'delfi:{self.run_for("t4")}:2026-11-05']['calls']
        self.assertEqual([c.get('arrivalDelay', c.get('departureDelay')) for c in night], [240, 240, 240])
        no_data = live_relay.runs_from_feed(self.schedule, message(trip('t1', '20261105', [stop(1, departure=event(60)), stop(2, relationship=2)])), 0)[1]
        self.assertEqual(no_data[f'delfi:{variant}:2026-11-05']['calls'], [{'sequence': 0, 'departureDelay': 60}])

    def test_scheduled_times_are_civil_across_midnight(self):
        variant = self.run_for('t4')
        # t4 leaves Berlin 23:50 and reaches Praha at 26:30 = 02:30 next day.
        self.assertEqual(self.schedule.scheduled(variant, dt.date(2026, 11, 5), 2)[0],
                         int(dt.datetime(2026, 11, 6, 2, 30, tzinfo=live_relay.ZONE).timestamp()))

    def test_http_lookup_validates_ids_and_keeps_aged_reports(self):
        relay = live_relay.Relay(pathlib.Path(self.temp.name), 60)
        relay.schedule = self.schedule
        variant = self.run_for('t1')
        relay.runs = {f'delfi:{variant}:2026-11-05': {'id': f'delfi:{variant}:2026-11-05', 'reportedAt': 1, 'calls': []}}
        server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), live_relay.handler(relay))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        base = f'http://127.0.0.1:{server.server_address[1]}'
        try:
            body = json.load(urllib.request.urlopen(f'{base}/v1/runs?ids=delfi:{variant}:2026-11-05,delfi:{"0"*16}:2026-11-05'))
            self.assertEqual([r['id'] for r in body['runs']], [f'delfi:{variant}:2026-11-05'])
            self.assertEqual(body['source']['license'], 'CC-BY-SA-4.0')
            for bad in ('cisjr:x:2026-11-05', '', ','.join(f'delfi:{i:016x}:2026-11-05' for i in range(101))):
                with self.assertRaises(urllib.error.HTTPError) as error: urllib.request.urlopen(f'{base}/v1/runs?ids={bad}')
                self.assertEqual(error.exception.code, 400)
            with self.assertRaises(urllib.error.HTTPError) as error: urllib.request.urlopen(f'{base}/v1/health')
            self.assertEqual(error.exception.code, 503)
        finally: server.shutdown()

if __name__ == '__main__': unittest.main()
