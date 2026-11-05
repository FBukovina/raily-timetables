"""Prague (PID) realtime for Raily, fetched once and shared by every phone.

Raily reads four Golemio endpoints: every vehicle position (2.5 MB of JSON for
about 2,200 buses, trams and trains, of which some 140 are trains), one
followed train, a trip's static timetable and station departure boards. Each
phone asked Golemio itself with the one key built into the app. Here the relay
asks instead, shares each answer for a few seconds and sends only trains from
the position list, so a phone downloads kilobytes and Golemio sees one request
per answer however many people look.

Answers keep Golemio's JSON as it is, so the app decodes them unchanged.
Proxied answers carry `X-Raily-Source: golemio`; anything without it (an old
relay, no key here, Golemio unreachable or refusing the key) tells the app to
ask Golemio directly. Nothing about a request is logged.

GET /v1/pid/vehiclepositions             trains only; offset > 0 is empty
GET /v1/pid/vehiclepositions/<trip>      one vehicle, 404 when not running
GET /v1/pid/gtfs/trips/<trip>            static trip, stops, times and shape
GET /v1/pid/departureboards?ids[]=...    the app's two board queries
"""
from __future__ import annotations
import collections, gzip, json, pathlib, re, threading, time, urllib.error, urllib.parse, urllib.request

GOLEMIO = 'https://api.golemio.cz'
SOURCE = ('X-Raily-Source', 'golemio')
USER_AGENT = 'raily-live-relay (+https://github.com/FBukovina/raily-timetables)'
TRIP_ID = re.compile(r'^[A-Za-z0-9_.-]{1,80}$')
STOP_ID = re.compile(r'^[A-Za-z0-9_.-]{1,40}$')
PAGE = 10_000
MAX_BYTES = 48 * 1024 * 1024
# Seconds an answer is shared. The app refreshes the map every 30 s and a
# followed train every 20 s; timetables are static.
POSITIONS, POSITION, TRIP, BOARD, MISSING, FAILED = 15, 10, 6 * 3600, 20, 60, 5
BOARD_CHOICES = {'mode': {'mixed', 'departures', 'arrivals'}, 'order': {'real', 'timetable'},
                 'preferredTimezone': {'Europe_Prague'}}
BOARD_NUMBERS = {'minutesBefore': (0, 360), 'minutesAfter': (0, 720), 'limit': (1, 1000)}
TRIP_FLAGS = {'includeStops', 'includeStopTimes', 'includeRoute', 'includeShapes'}


class Answer:
    """A finished HTTP answer, compressed once for every phone that gets it."""
    def __init__(self, status, body, proxied):
        self.status, self.body, self.proxied = status, body, proxied
        self.gzipped = gzip.compress(body, mtime=0) if len(body) > 1024 else None

    @classmethod
    def json(cls, status, value, proxied=False):
        return cls(status, json.dumps(value, separators=(',', ':')).encode(), proxied)

    def size(self): return len(self.body) + len(self.gzipped or b'')


class Shared:
    """Answers kept for their lifetime, bounded by count and bytes. Concurrent
    requests for a missing answer wait for one upstream request."""
    def __init__(self, entries=2000, size=256 * 1024 * 1024, clock=time.monotonic):
        self.entries, self.limit, self.clock = entries, size, clock
        self.lock, self.values, self.pending, self.bytes = threading.Lock(), collections.OrderedDict(), {}, 0

    def get(self, key, load):
        while True:
            with self.lock:
                hit = self.values.get(key)
                if hit and hit[0] > self.clock():
                    self.values.move_to_end(key); return hit[1]
                waiting = self.pending.get(key)
                if waiting is None: self.pending[key] = threading.Event()
            if waiting is None: break
            waiting.wait(90)
        try:
            answer, lifetime = load()
            with self.lock:
                if old := self.values.pop(key, None): self.bytes -= old[1].size()
                self.values[key] = (self.clock() + lifetime, answer); self.bytes += answer.size()
                while self.values and (len(self.values) > self.entries or self.bytes > self.limit):
                    self.bytes -= self.values.popitem(last=False)[1][1].size()
            return answer
        finally:
            with self.lock: self.pending.pop(key).set()


def golemio(key, path, query, timeout=25):
    """Golemio's status and body. The key travels only in the header."""
    url = GOLEMIO + path + ('?' + urllib.parse.urlencode(query, safe='[]') if query else '')
    request = urllib.request.Request(url, headers={'X-Access-Token': key, 'Accept': 'application/json',
                                                   'Accept-Encoding': 'gzip', 'User-Agent': USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            data = response.read(MAX_BYTES + 1)
            if len(data) > MAX_BYTES: raise ValueError('response too large')
            if response.headers.get('Content-Encoding') == 'gzip': data = gzip.decompress(data)
            return response.status, data
    except urllib.error.HTTPError as error:
        return error.code, b''


class PID:
    def __init__(self, key, fetch=golemio, shared=None):
        self.key, self.fetch, self.shared = key, fetch, shared or Shared()
        self.lock, self.last_success, self.last_error, self.last_error_at = threading.Lock(), None, None, None

    @classmethod
    def from_file(cls, path):
        """None without a key file: the app then keeps asking Golemio itself."""
        try: key = pathlib.Path(path).read_text().strip() if path else ''
        except OSError: key = ''
        return cls(key) if key else None

    def health(self):
        with self.lock:
            return {'lastSuccess': int(self.last_success) if self.last_success else None, 'lastError': self.last_error,
                    'lastErrorAt': int(self.last_error_at) if self.last_error_at else None}

    def answer(self, path, query):
        """The answer for a /v1/pid path, or None for another path."""
        params = urllib.parse.parse_qsl(query, keep_blank_values=True)
        parts = path.split('/')[3:]
        if parts == ['vehiclepositions']: return self.positions(params)
        if len(parts) == 2 and parts[0] == 'vehiclepositions': return self.position(urllib.parse.unquote(parts[1]), params)
        if len(parts) == 3 and parts[:2] == ['gtfs', 'trips']: return self.trip(urllib.parse.unquote(parts[2]), params)
        if parts == ['departureboards']: return self.board(params)
        return None

    def positions(self, params):
        values = dict(params)
        if len(values) != len(params) or not set(values) <= {'limit', 'offset', 'preferredTimezone'} \
                or values.get('preferredTimezone', 'Europe_Prague') != 'Europe_Prague' \
                or not all(values.get(k, '0').isdigit() for k in ('limit', 'offset')):
            return Answer.json(400, {'error': 'limit, offset and preferredTimezone=Europe_Prague only'})
        # Every train fits on the first page; the app's next page ends its loop.
        if int(values.get('offset', '0')) > 0: return Answer.json(200, {'type': 'FeatureCollection', 'features': []}, True)
        return self.shared.get(('positions',), self.load_positions)

    def load_positions(self):
        features, offset = [], 0
        while True:
            status, data = self.call('/v2/vehiclepositions', [('limit', PAGE), ('offset', offset), ('preferredTimezone', 'Europe_Prague')])
            if status != 200: return self.failure(status)
            try:
                page = json.loads(data)
                if page.get('type') != 'FeatureCollection' or not isinstance(page.get('features'), list): raise ValueError
            except ValueError:
                return self.failure('invalid JSON')
            features += page['features']
            if len(page['features']) < PAGE or offset >= 10 * PAGE: break
            offset += PAGE
        # The app's railway test: GTFS route_type 2. It applies its own checks too.
        trains = [f for f in features if isinstance(f, dict) and (((f.get('properties') or {}).get('trip') or {}).get('gtfs') or {}).get('route_type') == 2]
        return self.success(Answer.json(200, {'type': 'FeatureCollection', 'features': trains}, True), POSITIONS)

    def position(self, trip_id, params):
        if not TRIP_ID.match(trip_id) or params not in ([], [('preferredTimezone', 'Europe_Prague')]):
            return Answer.json(400, {'error': 'trip ID and preferredTimezone=Europe_Prague only'})
        return self.shared.get(('position', trip_id), lambda: self.passed(
            '/v2/vehiclepositions/' + urllib.parse.quote(trip_id, safe=''), [('preferredTimezone', 'Europe_Prague')], POSITION))

    def trip(self, trip_id, params):
        flags = {k for k, v in params if v == 'true'}
        if not TRIP_ID.match(trip_id) or len(flags) != len(params) or not flags <= TRIP_FLAGS:
            return Answer.json(400, {'error': f'trip ID and {", ".join(sorted(TRIP_FLAGS))}=true only'})
        query = [(k, 'true') for k in sorted(flags)]
        return self.shared.get(('trip', trip_id, *sorted(flags)), lambda: self.passed(
            '/v2/gtfs/trips/' + urllib.parse.quote(trip_id, safe=''), query, TRIP))

    def board(self, params):
        ids = sorted({v for k, v in params if k == 'ids[]'})
        rest = [(k, v) for k, v in params if k != 'ids[]']
        values = dict(rest)
        valid = 0 < len(ids) <= 100 and all(STOP_ID.match(i) for i in ids) and len(values) == len(rest) \
            and all(k in BOARD_CHOICES and v in BOARD_CHOICES[k] or k in BOARD_NUMBERS and v.isdigit()
                    and BOARD_NUMBERS[k][0] <= int(v) <= BOARD_NUMBERS[k][1] for k, v in rest)
        if not valid: return Answer.json(400, {'error': 'ids[] (1 to 100) and the board options Raily uses'})
        query = [('ids[]', i) for i in ids] + sorted(rest)
        return self.shared.get(('board', *query), lambda: self.passed('/v2/pid/departureboards', query, BOARD))

    def passed(self, path, query, lifetime):
        """Golemio's answer as it is, for statuses that describe the request."""
        status, data = self.call(path, query)
        if status == 200:
            try: json.loads(data)
            except ValueError: return self.failure('invalid JSON')
            return self.success(Answer(200, data, True), lifetime)
        if status == 404: return self.success(Answer.json(404, {'error': 'not found'}, True), MISSING)
        if status in (400, 429): return self.success(Answer.json(status, {'error': 'Golemio refused the request'}, True), FAILED)
        return self.failure(status)

    def call(self, path, query):
        try: return self.fetch(self.key, path, query)
        except Exception as error: return type(error).__name__, b''

    def success(self, answer, lifetime):
        with self.lock: self.last_success = time.time()
        return answer, lifetime

    def failure(self, reason):
        # Authentication, server and network failures: the app asks Golemio itself.
        with self.lock: self.last_error, self.last_error_at = f'Golemio: {reason}', time.time()
        return Answer.json(502, {'error': 'Golemio is unavailable through the relay'}), FAILED
