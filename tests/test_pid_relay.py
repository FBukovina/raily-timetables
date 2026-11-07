import gzip, json, pathlib, sys, threading, time, unittest, urllib.error, urllib.request, http.server
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / 'scripts'))
import live_relay
from pid_relay import PID, Shared

def vehicle(trip_id, route_type):
    return {'type': 'Feature', 'geometry': None,
            'properties': {'trip': {'gtfs': {'trip_id': trip_id, 'route_type': route_type}}, 'last_position': None}}

class Golemio:
    """Answers like Golemio and records every request the relay makes."""
    def __init__(self, answers=None):
        self.calls, self.answers, self.lock = [], answers or {}, threading.Lock()

    def __call__(self, key, path, query):
        with self.lock: self.calls.append((key, path, list(query)))
        if path == '/v2/vehiclepositions':
            offset = dict(query)['offset']
            features = [vehicle('rail-1', 2), vehicle('bus-1', 3), vehicle('tram-1', 0)] if offset == 0 else []
            return 200, json.dumps({'type': 'FeatureCollection', 'features': features}).encode()
        answer = self.answers.get(path, (404, b''))
        if isinstance(answer, Exception): raise answer
        return answer

class PIDRelayTests(unittest.TestCase):
    def test_positions_keep_only_trains_and_are_shared(self):
        golemio = Golemio(); pid = PID('key', golemio)
        body = json.loads(pid.answer('/v1/pid/vehiclepositions', 'limit=10000&offset=0&preferredTimezone=Europe_Prague').body)
        self.assertEqual([f['properties']['trip']['gtfs']['trip_id'] for f in body['features']], ['rail-1'])
        pid.answer('/v1/pid/vehiclepositions', 'limit=10000&offset=0&preferredTimezone=Europe_Prague')
        self.assertEqual(len(golemio.calls), 1, 'a second phone gets the shared answer')
        self.assertEqual(golemio.calls[0][0], 'key')
        later = pid.answer('/v1/pid/vehiclepositions', 'limit=10000&offset=10000&preferredTimezone=Europe_Prague')
        self.assertEqual(json.loads(later.body)['features'], [])
        self.assertEqual(len(golemio.calls), 1, 'later pages never reach Golemio')

    def test_concurrent_requests_wait_for_one_upstream_request(self):
        release = threading.Event(); golemio = Golemio()
        def slow(key, path, query): release.wait(5); return golemio(key, path, query)
        pid = PID('key', slow); answers = []
        threads = [threading.Thread(target=lambda: answers.append(pid.answer('/v1/pid/vehiclepositions', ''))) for _ in range(8)]
        for t in threads: t.start()
        time.sleep(0.2); release.set()
        for t in threads: t.join(5)
        self.assertEqual(len(answers), 8)
        self.assertEqual({a.status for a in answers}, {200})
        self.assertEqual(len(golemio.calls), 1)

    def test_shared_answers_expire_and_stay_within_their_bounds(self):
        now = [0.0]; golemio = Golemio({'/v2/vehiclepositions/t1': (200, b'{"type":"Feature"}')})
        pid = PID('key', golemio, Shared(entries=2, clock=lambda: now[0]))
        pid.answer('/v1/pid/vehiclepositions/t1', 'preferredTimezone=Europe_Prague')
        now[0] = 9; pid.answer('/v1/pid/vehiclepositions/t1', 'preferredTimezone=Europe_Prague')
        self.assertEqual(len(golemio.calls), 1)
        now[0] = 11; pid.answer('/v1/pid/vehiclepositions/t1', 'preferredTimezone=Europe_Prague')
        self.assertEqual(len(golemio.calls), 2, 'a followed train is shared for 10 seconds')
        for trip in ('t2', 't3'): pid.answer(f'/v1/pid/vehiclepositions/{trip}', '')
        self.assertEqual(len(pid.shared.values), 2)

    def test_golemio_answers_pass_through_with_the_source_header(self):
        trip = b'{"stops":[],"stop_times":[]}'
        golemio = Golemio({'/v2/gtfs/trips/1116_567_251214': (200, trip), '/v2/pid/departureboards': (429, b'')})
        pid = PID('key', golemio)
        answer = pid.answer('/v1/pid/gtfs/trips/1116_567_251214', 'includeStops=true&includeStopTimes=true&includeRoute=true&includeShapes=true')
        self.assertEqual((answer.status, answer.body, answer.proxied), (200, trip, True))
        self.assertEqual(golemio.calls[-1][1:], ('/v2/gtfs/trips/1116_567_251214',
                         [('includeRoute', 'true'), ('includeShapes', 'true'), ('includeStopTimes', 'true'), ('includeStops', 'true')]))
        missing = pid.answer('/v1/pid/vehiclepositions/gone', 'preferredTimezone=Europe_Prague')
        self.assertEqual((missing.status, missing.proxied), (404, True), 'a train not running is an answer')
        busy = pid.answer('/v1/pid/departureboards', 'ids[]=U142Z301&mode=departures&minutesBefore=0&minutesAfter=360&limit=1000&order=real&preferredTimezone=Europe_Prague')
        self.assertEqual((busy.status, busy.proxied), (429, True))

    def test_failures_without_the_header_send_the_app_to_golemio(self):
        golemio = Golemio({'/v2/vehiclepositions/t1': (403, b''), '/v2/vehiclepositions/t2': (503, b''),
                           '/v2/vehiclepositions/t3': TimeoutError('timed out'), '/v2/vehiclepositions/t4': (200, b'<html>')})
        pid = PID('key', golemio)
        for trip in ('t1', 't2', 't3', 't4'):
            answer = pid.answer(f'/v1/pid/vehiclepositions/{trip}', '')
            self.assertEqual((answer.status, answer.proxied), (502, False), trip)
        self.assertEqual(pid.health()['lastError'], 'Golemio: invalid JSON')

    def test_requests_are_limited_to_what_the_app_asks(self):
        golemio = Golemio(); pid = PID('key', golemio)
        board = 'mode=departures&minutesBefore=0&minutesAfter=360&limit=1000&order=real&preferredTimezone=Europe_Prague'
        bad = [('/v1/pid/vehiclepositions', 'preferredTimezone=Europe_Berlin'),
               ('/v1/pid/vehiclepositions', 'updatedSince=2026-11-05'),
               ('/v1/pid/vehiclepositions/a%2Fb', ''),
               ('/v1/pid/gtfs/trips/t1', 'includeStops=false'),
               ('/v1/pid/departureboards', board),
               ('/v1/pid/departureboards', 'ids[]=U1&' + board.replace('360', '9999')),
               ('/v1/pid/departureboards', 'ids[]=U1&mode=departures&mode=arrivals'),
               ('/v1/pid/departureboards', '&'.join(f'ids[]=U{i}' for i in range(101)))]
        for path, query in bad:
            self.assertEqual(pid.answer(path, query).status, 400, (path, query))
        self.assertEqual(golemio.calls, [])
        self.assertIsNone(pid.answer('/v1/pid/other', ''))
        pid.answer('/v1/pid/departureboards', 'ids[]=U2&ids[]=U1&ids[]=U2&' + board)
        self.assertEqual(golemio.calls[0][2][:2], [('ids[]', 'U1'), ('ids[]', 'U2')])

    def test_http_marks_proxied_answers_and_works_without_a_key(self):
        golemio = Golemio()
        for pid, status, header in ((PID('key', golemio), 200, 'golemio'), (None, 503, None)):
            relay = live_relay.Relay(pathlib.Path('.'), 60, pid=pid)
            server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), live_relay.handler(relay))
            threading.Thread(target=server.serve_forever, daemon=True).start()
            url = f'http://127.0.0.1:{server.server_address[1]}/v1/pid/vehiclepositions'
            try:
                try: response = urllib.request.urlopen(urllib.request.Request(url, headers={'Accept-Encoding': 'gzip'}))
                except urllib.error.HTTPError as error: response = error
                self.assertEqual(response.status, status)
                self.assertEqual(response.headers.get('X-Raily-Source'), header)
                if status == 200:
                    body = response.read()
                    if response.headers.get('Content-Encoding') == 'gzip': body = gzip.decompress(body)
                    self.assertEqual(len(json.loads(body)['features']), 1)
            finally: server.shutdown(); server.server_close()

    def test_key_file(self):
        self.assertIsNone(PID.from_file(None))
        self.assertIsNone(PID.from_file('/nonexistent/key'))

if __name__ == '__main__': unittest.main()
