#!/usr/bin/env python3
"""Raily Live: realtime predictions for Raily's catalog runs, served by run ID.

Germany's free realtime stream (gtfs.de, DELFI e.V. data, CC BY-SA 4.0) is a
12.8 MB gzip snapshot of 126 k trips every 10 seconds, too large for a phone.
The relay fetches it once a minute, keeps the railway trips of the same static
feeds that build catalog/de/v2, and places each update on the catalog's calls:
the run ID is the importer's content hash plus the service date, so
`delfi:<variant>:<YYYY-MM-DD>` here is exactly the app's run identity.

Updates are predictions, not observations. A stop without its own update takes
the delay of the preceding update, as GTFS Realtime specifies, and is marked
`propagated`. A trip missing from the feed says nothing about the train; its
last report is kept until it is 20 minutes old.

    python3 scripts/download_gtfs.py --country de
    python3 scripts/live_relay.py --port 8080

GET /v1/runs?ids=<run>,<run>  up to 100 runs; unknown runs are left out
GET /v1/health                feed and schedule state, for monitoring
"""
from __future__ import annotations
import argparse, datetime as dt, gzip, http.server, json, pathlib, re, sys, threading, time, urllib.error, urllib.parse, urllib.request
from zoneinfo import ZoneInfo
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from gtfs_catalog import Borders, read_feeds
from gtfs_realtime import CANCELED, NO_DATA, SKIPPED, decode

FEED_URL = 'https://realtime.gtfs.de/realtime-free.pb'
ATTRIBUTION = {'name': 'DELFI e.V., gtfs.de', 'license': 'CC-BY-SA-4.0', 'url': 'https://gtfs.de/en/realtime/'}
ZONE = ZoneInfo('Europe/Berlin')
RUN_ID = re.compile(r'^delfi:[0-9a-f]{16}:\d{4}-\d{2}-\d{2}$')
MAX_IDS = 100
KEEP_SECONDS = 20 * 60
USER_AGENT = 'raily-live-relay (+https://github.com/FBukovina/raily-timetables)'


class Schedule:
    """The railway trips of the static feeds, keyed for realtime joins."""

    def __init__(self, feeds):
        self.trips = {}
        _, variants, _, _ = read_feeds(feeds, 'DE', Borders(), trip_runs=self.trips)
        # Per variant: start second and each call's (arrival, departure) offset.
        self.calls = {vid: (v['start'], tuple((p[1], p[2]) for p in v['pattern'])) for vid, v in variants.items()}

    def scheduled(self, variant, day, index):
        """Scheduled (arrival, departure) epoch seconds of a call, as civil time past midnight."""
        start, calls = self.calls[variant]
        midnight = dt.datetime.combine(day, dt.time(), ZONE)
        def at(offset):
            if offset is None: return None
            elapsed = start + offset
            local = midnight + dt.timedelta(days=elapsed // 86400, seconds=elapsed % 86400)
            # Civil wall time: rebuild from fields so a clock change shifts nothing.
            return int(dt.datetime(local.year, local.month, local.day, local.hour, local.minute, local.second, tzinfo=ZONE).timestamp())
        arrival, departure = calls[index]
        return at(arrival), at(departure)


def _delay(event, scheduled):
    if event is None: return None
    if 'delay' in event: return event['delay']
    if 'time' in event and scheduled is not None: return event['time'] - scheduled
    return None


def runs_from_feed(schedule, data, received_at):
    """Decode one feed snapshot into {run ID: run} for the schedule's trips."""
    header, updates = decode(data, wanted=schedule.trips.__contains__)
    runs = {}
    for update in updates:
        variant, sequences = schedule.trips[update['trip_id']]
        date = update.get('start_date') or ''
        if not re.fullmatch(r'\d{8}', date): continue
        try: day = dt.date(int(date[:4]), int(date[4:6]), int(date[6:]))
        except ValueError: continue
        run_id = f'delfi:{variant}:{day.isoformat()}'
        index_of = {sequence: index for index, sequence in enumerate(sequences)}
        _, call_offsets = schedule.calls[variant]
        explicit = {}
        for stop in update['stops']:
            index = index_of.get(stop.get('sequence'))
            if index is None: continue
            relationship = stop.get('relationship', 0)
            if relationship == NO_DATA:
                explicit[index] = None
                continue
            arrival_at, departure_at = schedule.scheduled(variant, day, index)
            call = {'sequence': index}
            if relationship == SKIPPED: call['skipped'] = True
            arrival = _delay(stop.get('arrival'), arrival_at)
            departure = _delay(stop.get('departure'), departure_at)
            if arrival is not None and call_offsets[index][0] is not None: call['arrivalDelay'] = arrival
            if departure is not None and call_offsets[index][1] is not None: call['departureDelay'] = departure
            explicit[index] = call
        calls, carried = [], None
        if not explicit and 'delay' in update:
            carried, start = update['delay'], 0
        else:
            start = min(explicit) if explicit else len(call_offsets)
        for index in range(start, len(call_offsets)):
            if index in explicit:
                call = explicit[index]
                if call is None:
                    carried = None
                    continue
                calls.append(call)
                if not call.get('skipped'):
                    carried = call.get('departureDelay', call.get('arrivalDelay', carried))
            elif carried is not None:
                arrival_offset, departure_offset = call_offsets[index]
                call = {'sequence': index, 'propagated': True}
                if arrival_offset is not None: call['arrivalDelay'] = carried
                if departure_offset is not None: call['departureDelay'] = carried
                calls.append(call)
        run = {'id': run_id, 'reportedAt': update.get('timestamp') or header or int(received_at), 'calls': calls}
        if update.get('relationship') == CANCELED: run['cancelled'] = True
        runs[run_id] = run
    return header, runs


class Relay:
    def __init__(self, cache, interval, feed_url=FEED_URL):
        self.cache, self.interval, self.feed_url = cache, interval, feed_url
        self.lock = threading.Lock()
        self.schedule = None
        self.runs, self.feed_timestamp, self.fetched_at, self.etag = {}, None, None, None
        self.schedule_inventory, self.last_error = None, None

    def load_schedule(self):
        from download_gtfs import download
        inventory = download('de', self.cache)
        signature = {name: item['sha256'] for name, item in inventory['feeds'].items()}
        if signature == self.schedule_inventory: return
        schedule = Schedule([self.cache / name for name in sorted(inventory['feeds'])])
        with self.lock:
            self.schedule, self.schedule_inventory = schedule, signature
        print(f'Schedule: {len(schedule.trips)} railway trips', flush=True)

    def fetch(self):
        request = urllib.request.Request(self.feed_url, headers={'Accept-Encoding': 'gzip', 'User-Agent': USER_AGENT})
        if self.etag: request.add_header('If-None-Match', self.etag)
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                data = response.read(64 * 1024 * 1024)
                if response.headers.get('Content-Encoding') == 'gzip': data = gzip.decompress(data)
                etag = response.headers.get('ETag')
        except urllib.error.HTTPError as error:
            if error.code == 304: return
            raise
        now = time.time()
        header, runs = runs_from_feed(self.schedule, data, now)
        with self.lock:
            # A trip missing from this snapshot keeps its last report until it ages out.
            kept = {k: v for k, v in self.runs.items() if k not in runs and now - v['reportedAt'] < KEEP_SECONDS}
            self.runs = {**kept, **runs}
            self.feed_timestamp, self.fetched_at, self.etag, self.last_error = header, now, etag, None

    def loop(self):
        next_schedule = 0
        while True:
            started = time.time()
            try:
                if started >= next_schedule:
                    self.load_schedule(); next_schedule = started + 6 * 3600
                self.fetch()
            except Exception as error:  # Keep serving the last good state.
                with self.lock: self.last_error = f'{type(error).__name__}: {error}'
                print(f'Feed error: {self.last_error}', file=sys.stderr, flush=True)
            time.sleep(max(5, self.interval - (time.time() - started)))

    def lookup(self, ids):
        with self.lock:
            return {'source': ATTRIBUTION, 'feedTimestamp': self.feed_timestamp,
                    'fetchedAt': int(self.fetched_at) if self.fetched_at else None,
                    'runs': [self.runs[i] for i in ids if i in self.runs]}

    def health(self):
        with self.lock:
            fresh = self.fetched_at is not None and time.time() - self.fetched_at < 5 * 60
            return {'ok': fresh and self.schedule is not None, 'feedTimestamp': self.feed_timestamp,
                    'fetchedAt': int(self.fetched_at) if self.fetched_at else None, 'runs': len(self.runs),
                    'trips': len(self.schedule.trips) if self.schedule else 0, 'lastError': self.last_error}


def handler(relay):
    class Handler(http.server.BaseHTTPRequestHandler):
        server_version = 'RailyLive/1'

        def do_GET(self):
            url = urllib.parse.urlsplit(self.path)
            if url.path == '/v1/health':
                body = relay.health(); return self.reply(200 if body['ok'] else 503, body, cache=0)
            if url.path != '/v1/runs': return self.reply(404, {'error': 'not found'}, cache=300)
            raw = urllib.parse.parse_qs(url.query).get('ids', [''])[0]
            ids = list(dict.fromkeys(i for i in raw.split(',') if i))
            if not ids or len(ids) > MAX_IDS or not all(RUN_ID.match(i) for i in ids):
                return self.reply(400, {'error': f'ids: 1 to {MAX_IDS} run IDs'}, cache=300)
            self.reply(200, relay.lookup(ids), cache=15)

        def reply(self, status, body, cache):
            data = json.dumps(body, separators=(',', ':')).encode()
            gzipped = 'gzip' in self.headers.get('Accept-Encoding', '') and len(data) > 1024
            if gzipped: data = gzip.compress(data, mtime=0)
            self.send_response(status)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Cache-Control', f'public, max-age={cache}' if cache else 'no-store')
            if gzipped: self.send_header('Content-Encoding', 'gzip')
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args):
            # No client addresses or run IDs are logged.
            pass
    return Handler


if __name__ == '__main__':
    p = argparse.ArgumentParser()
    p.add_argument('--cache', type=pathlib.Path, default=pathlib.Path('.cache/gtfs/de'))
    p.add_argument('--host', default='127.0.0.1'); p.add_argument('--port', type=int, default=8080)
    p.add_argument('--interval', type=int, default=60, help='Seconds between feed fetches')
    a = p.parse_args()
    relay = Relay(a.cache, a.interval)
    threading.Thread(target=relay.loop, daemon=True).start()
    server = http.server.ThreadingHTTPServer((a.host, a.port), handler(relay))
    print(f'Raily Live on http://{a.host}:{a.port}', flush=True)
    server.serve_forever()
