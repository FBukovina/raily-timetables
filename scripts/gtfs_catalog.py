#!/usr/bin/env python3
"""Railway GTFS feeds -> a Raily country catalog (schema 2 queries, pattern storage).

The app reads every country catalog with the same schema 2 SQL. GTFS trips
repeat a few stop patterns at many start times, so calls are stored once per
pattern and `calls` is a view: Germany's regional feed has 1.58 M stop times
but 397 k pattern calls.

Times stay civil: GTFS HH:MM:SS past midnight of the service day, split into
seconds and a day offset exactly like CIS calls. Clock-change nights are
resolved conservatively by the app. Trips whose times run backwards are
quarantined with their identifiers, never repaired.
"""
from __future__ import annotations
import argparse, collections, csv, datetime as dt, hashlib, io, json, pathlib, re, sqlite3, unicodedata, zipfile
from catalog import day_calendar, package

TABLES = '''
PRAGMA user_version=2;
PRAGMA foreign_keys=ON;
CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL) WITHOUT ROWID;
CREATE TABLE stations(id TEXT PRIMARY KEY,name TEXT NOT NULL,normalized_name TEXT NOT NULL,country TEXT NOT NULL,code TEXT NOT NULL,latitude REAL,longitude REAL) WITHOUT ROWID;
CREATE TABLE pattern_calls(pattern_id INTEGER NOT NULL,sequence INTEGER NOT NULL,station_id TEXT NOT NULL REFERENCES stations(id),arrival_offset INTEGER,departure_offset INTEGER,allows_boarding INTEGER NOT NULL CHECK(allows_boarding IN(0,1)),allows_alighting INTEGER NOT NULL CHECK(allows_alighting IN(0,1)),PRIMARY KEY(pattern_id,sequence),CHECK(arrival_offset IS NULL OR arrival_offset>=0),CHECK(departure_offset IS NULL OR departure_offset>=0)) WITHOUT ROWID;
CREATE TABLE variants(id TEXT PRIMARY KEY,train_number TEXT NOT NULL,title TEXT NOT NULL,operator_name TEXT NOT NULL,destination TEXT NOT NULL,facilities_json TEXT NOT NULL DEFAULT '[]',pattern_id INTEGER NOT NULL,start_seconds INTEGER NOT NULL CHECK(start_seconds BETWEEN 0 AND 172799)) WITHOUT ROWID;
CREATE TABLE variant_days(variant_id TEXT PRIMARY KEY REFERENCES variants(id),first_day TEXT NOT NULL,last_day TEXT NOT NULL,days TEXT NOT NULL,CHECK(date(first_day) IS first_day AND date(last_day) IS last_day),CHECK(length(days)=julianday(last_day)-julianday(first_day)+1),CHECK(days GLOB '1*' AND days GLOB '*1' AND NOT days GLOB '*[^01]*')) WITHOUT ROWID;
CREATE INDEX stations_name ON stations(normalized_name);
CREATE INDEX variants_number ON variants(train_number);
CREATE INDEX variants_pattern ON variants(pattern_id);
CREATE INDEX pattern_calls_station ON pattern_calls(station_id,pattern_id,sequence);
CREATE VIEW calls AS SELECT v.id AS variant_id,p.sequence AS sequence,p.station_id AS station_id,
  (v.start_seconds+p.arrival_offset)%86400 AS arrival_seconds,(v.start_seconds+p.arrival_offset)/86400 AS arrival_day_offset,
  (v.start_seconds+p.departure_offset)%86400 AS departure_seconds,(v.start_seconds+p.departure_offset)/86400 AS departure_day_offset,
  p.allows_boarding AS allows_boarding,p.allows_alighting AS allows_alighting
  FROM variants v JOIN pattern_calls p ON p.pattern_id=v.pattern_id;
'''
# GTFS route types for passenger railway: basic 2 and the extended 100-117 railway family.
RAIL_TYPES = {2} | set(range(100, 118))
BORDERS = pathlib.Path(__file__).with_name('europe-borders.json')

def normalized(value):
    # casefold, not lower: the app's Foundation folding turns ß into ss.
    return ''.join(c for c in unicodedata.normalize('NFKD', value.casefold()) if not unicodedata.combining(c) and c.isalnum())

def display_name(source):
    """Regional systems name stations differently ("Dülmen, Bahnhof", "S+U Berlin
    Hauptbahnhof", "Hamburg, Hamburg Hbf"). Show the short railway form; the
    source name stays searchable and identity never depends on this."""
    name = ' '.join(source.split())
    name = re.sub(r'^(S\+U|S|U) (?=[A-ZÄÖÜ])', '', name)
    if ',' in name:
        town, _, stop = (part.strip() for part in name.partition(','))
        if stop in ('Bahnhof', 'Bf', 'Bf.', 'Haltepunkt'): name = town
        elif town and stop.startswith(town): name = stop
        elif town and stop: name = town + ' ' + stop
    # VBB names Berlin's stations "Südkreuz Bhf (Berlin)"; the railway name is "Berlin Südkreuz".
    name = re.sub(r'^(.+?)(?: Bhf)? \(Berlin\)$', r'Berlin \1', name)
    name = re.sub(r'\bHauptbahnhof\b', 'Hbf', name)
    name = re.sub(r' (?:Bahnhof|Bf\.?|Bhf)(?= \(|$)', '', name)
    name = re.sub(r' Bahnhof (?=\S)', ' ', name)
    return name.strip() or source.strip()

def operator_name(source):
    """DELFI prefixes many operators with a six-character code ("800153 DB Regio AG Nordost")."""
    return re.sub(r'^(?=[0-9A-Z]*[0-9])[0-9A-Z]{6} (?=\S)', '', source.strip())

def search_text(name, source):
    """Space-separated folded aliases; search terms never contain a space."""
    aliases = [normalized(name), normalized(source), normalized(name.replace('Hbf', 'Hauptbahnhof'))]
    return ' '.join(dict.fromkeys(a for a in aliases if a))

def seconds(value):
    if not value: return None
    hours, minutes, secs = (int(part) for part in value.strip().split(':'))
    if not (0 <= minutes < 60 and 0 <= secs < 60 and 0 <= hours < 168): raise ValueError(f'Invalid GTFS time {value}')
    return hours*3600 + minutes*60 + secs

def gtfs_date(value):
    return dt.date(int(value[:4]), int(value[4:6]), int(value[6:8]))

class Borders:
    """Point-in-polygon country lookup with bounding-box prefilters."""
    def __init__(self, path=BORDERS):
        self.polygons = []
        for code, polygons in json.loads(path.read_text(encoding='utf-8'))['countries'].items():
            for rings in polygons:
                xs = [p[0] for p in rings[0]]; ys = [p[1] for p in rings[0]]
                self.polygons.append((code, (min(xs), min(ys), max(xs), max(ys)), rings))

    @staticmethod
    def contains(ring, x, y):
        inside = False; j = len(ring) - 1
        for i in range(len(ring)):
            xi, yi = ring[i]; xj, yj = ring[j]
            if (yi > y) != (yj > y) and x < (xj - xi) * (y - yi) / (yj - yi) + xi: inside = not inside
            j = i
        return inside

    def country(self, latitude, longitude):
        for code, (x1, y1, x2, y2), rings in self.polygons:
            if x1 <= longitude <= x2 and y1 <= latitude <= y2 and self.contains(rings[0], longitude, latitude) \
               and not any(self.contains(hole, longitude, latitude) for hole in rings[1:]):
                return code
        return None

class Feed:
    def __init__(self, path):
        self.archive = zipfile.ZipFile(path); self.path = path

    def rows(self, name, required=True):
        if name not in self.archive.namelist():
            if required: raise ValueError(f'{self.path.name}: missing {name}')
            return
        with self.archive.open(name) as raw:
            yield from csv.DictReader(io.TextIOWrapper(raw, encoding='utf-8-sig', newline=''))

def operating_days(feed, first, last):
    days = collections.defaultdict(set)
    weekdays = ('monday', 'tuesday', 'wednesday', 'thursday', 'friday', 'saturday', 'sunday')
    for row in feed.rows('calendar.txt', required=False):
        start, end = max(gtfs_date(row['start_date']), first), min(gtfs_date(row['end_date']), last)
        day = start
        while day <= end:
            if row[weekdays[day.weekday()]] == '1': days[row['service_id']].add(day)
            day += dt.timedelta(days=1)
    for row in feed.rows('calendar_dates.txt', required=False):
        day = gtfs_date(row['date'])
        if not first <= day <= last: continue
        if row['exception_type'] == '1': days[row['service_id']].add(day)
        elif row['exception_type'] == '2': days[row['service_id']].discard(day)
        else: raise ValueError(f'Unknown calendar exception {row["exception_type"]}')
    return days

def feed_window(feed):
    starts, ends = [], []
    for row in feed.rows('calendar.txt', required=False):
        starts.append(gtfs_date(row['start_date'])); ends.append(gtfs_date(row['end_date']))
    for row in feed.rows('calendar_dates.txt', required=False):
        if row['exception_type'] == '1': starts.append(gtfs_date(row['date'])); ends.append(gtfs_date(row['date']))
    if not starts: raise ValueError(f'{feed.path.name}: no service calendar')
    return min(starts), max(ends)

def read_feeds(paths, home, borders, trip_runs=None):
    """Return stations, variants {id: record} and quarantined trips across all feeds.

    `trip_runs`, when given, receives each kept GTFS trip ID mapped to its variant ID
    and the GTFS stop_sequence of every passenger call, in call order. The live
    relay uses it to place realtime updates on catalog calls."""
    stations, variants, exclusions, counts = {}, {}, [], collections.Counter()
    for path in paths:
        feed = Feed(path)
        stops = {row['stop_id']: row for row in feed.rows('stops.txt')}
        def station_for(stop_id):
            row = stops[stop_id]; parent = row.get('parent_station') or ''
            return stops[parent] if parent in stops else row
        agencies = {row.get('agency_id', ''): operator_name(row['agency_name']) for row in feed.rows('agency.txt')}
        routes = {}
        for row in feed.rows('routes.txt'):
            if int(row['route_type']) not in RAIL_TYPES: continue
            agency = agencies.get(row.get('agency_id', ''), agencies.get('', ''))
            if not agency and len(agencies) == 1: agency = next(iter(agencies.values()))
            routes[row['route_id']] = ((row.get('route_short_name') or row.get('route_long_name') or '').strip(), agency)
        first, last = feed_window(feed)
        days = operating_days(feed, first, last)
        trips = {row['trip_id']: row for row in feed.rows('trips.txt') if row['route_id'] in routes}
        times = collections.defaultdict(list)
        for row in feed.rows('stop_times.txt'):
            if row['trip_id'] in trips: times[row['trip_id']].append(row)
        for trip_id, rows in times.items():
            trip = trips[trip_id]; counts['trips'] += 1
            title, operator = routes[trip['route_id']]
            service_days = days.get(trip['service_id'], set())
            if not service_days: counts['tripsWithoutDays'] += 1; continue
            rows.sort(key=lambda r: int(r['stop_sequence']))
            calls = []
            for row in rows:
                boarding, alighting = row.get('pickup_type', '') != '1', row.get('drop_off_type', '') != '1'
                if not boarding and not alighting: continue
                arrival, departure = seconds(row['arrival_time']), seconds(row['departure_time'])
                calls.append([station_for(row['stop_id']), arrival, departure, boarding, alighting, int(row['stop_sequence'])])
            reason = None
            if len(calls) < 2: reason = 'fewer than two passenger calls'
            else:
                calls[0][1] = None; calls[0][4] = False; calls[-1][2] = None; calls[-1][3] = False
                if calls[0][2] is None or calls[-1][1] is None: reason = 'untimed end call'
                previous = None
                for call in calls:
                    for value in (call[1], call[2]):
                        if value is None: continue
                        if previous is not None and value < previous: reason = 'times run backwards'
                        previous = value
                if not any(c[3] for c in calls): reason = 'no boarding call'
            if reason:
                exclusions.append({'feed': path.name, 'tripID': trip_id, 'reason': reason}); continue
            start = calls[0][2]
            if start >= 172800: exclusions.append({'feed': path.name, 'tripID': trip_id, 'reason': 'starts after the next day'}); continue
            pattern = tuple((c[0]['stop_id'], None if c[1] is None else c[1]-start, None if c[2] is None else c[2]-start,
                             int(c[3]), int(c[4])) for c in calls)
            for c in calls:
                row = c[0]; key = row['stop_id']
                if key not in stations:
                    latitude, longitude = float(row['stop_lat']), float(row['stop_lon'])
                    stations[key] = {'id': key, 'source': row['stop_name'].strip(), 'name': display_name(row['stop_name']),
                                     'latitude': latitude, 'longitude': longitude,
                                     'country': borders.country(latitude, longitude) or home}
            destination = display_name(calls[-1][0]['stop_name'])
            # Content identity: the same train in next week's export keeps its ID
            # even if the producer renumbers trip IDs.
            identity = json.dumps([operator, title, start, [(stations[p[0]]['source'],) + p[1:] for p in pattern]],
                                  ensure_ascii=False, separators=(',', ':'))
            variant_id = hashlib.sha256(identity.encode()).hexdigest()[:16]
            record = variants.get(variant_id)
            if record is None:
                variants[variant_id] = {'id': variant_id, 'title': title, 'operator': operator, 'destination': destination,
                                        'pattern': pattern, 'start': start, 'days': set(service_days)}
            elif record['pattern'] != pattern: raise ValueError(f'Variant identity collision {variant_id}')
            else: record['days'] |= service_days; counts['mergedTrips'] += 1
            if trip_runs is not None: trip_runs[trip_id] = (variant_id, tuple(c[5] for c in calls))
    return stations, variants, exclusions, counts

def write_catalog(stations, variants, output, metadata):
    temp = output.with_suffix('.partial'); temp.unlink(missing_ok=True)
    db = sqlite3.connect(temp); db.executescript(TABLES)
    db.executemany('INSERT INTO metadata VALUES(?,?)', ((k, str(v)) for k, v in metadata.items()))
    used = {p[0] for v in variants.values() for p in v['pattern']}
    db.executemany('INSERT INTO stations VALUES(?,?,?,?,?,?,?)', ((s['id'], s['name'], search_text(s['name'], s['source']), s['country'], s['id'],
        round(s['latitude'], 6), round(s['longitude'], 6)) for s in sorted(stations.values(), key=lambda s: s['id']) if s['id'] in used))
    patterns = {}
    for pattern in sorted({v['pattern'] for v in variants.values()}, key=lambda p: json.dumps(p)):
        patterns[pattern] = len(patterns) + 1
        db.executemany('INSERT INTO pattern_calls VALUES(?,?,?,?,?,?,?)', ((patterns[pattern], sequence) + call for sequence, call in enumerate(pattern)))
    for variant in sorted(variants.values(), key=lambda v: v['id']):
        db.execute('INSERT INTO variants VALUES(?,?,?,?,?,?,?,?)', (variant['id'], '', variant['title'], variant['operator'],
            variant['destination'], '[]', patterns[variant['pattern']], variant['start']))
        db.execute('INSERT INTO variant_days VALUES(?,?,?,?)', (variant['id'], *day_calendar(d.isoformat() for d in variant['days'])))
    if db.execute('PRAGMA integrity_check').fetchone()[0] != 'ok' or db.execute('PRAGMA foreign_key_check').fetchall():
        raise ValueError('SQLite validation failed')
    counts = {'stations': db.execute('SELECT count(*) FROM stations').fetchone()[0], 'variants': len(variants),
              'patterns': len(patterns), 'patternCalls': sum(len(p) for p in patterns),
              'calls': sum(len(v['pattern']) for v in variants.values()),
              'serviceDays': sum(len(v['days']) for v in variants.values())}
    db.commit(); db.execute('VACUUM'); db.close(); temp.replace(output)
    return counts

def build(feeds, output, base_url, home, source_url, source_checked_at, minimum_stations, minimum_variants):
    stations, variants, exclusions, counts = read_feeds(feeds, home, Borders())
    if not variants: raise ValueError('Empty catalog')
    days = [d for v in variants.values() for d in v['days']]
    now = dt.datetime.now(dt.timezone.utc).isoformat(timespec='seconds').replace('+00:00', 'Z')
    metadata = {'schemaVersion': 2, 'validFrom': min(days).isoformat(), 'validThrough': max(days).isoformat(),
                'sourceCheckedAt': source_checked_at, 'generatedAt': now, 'sourceURL': source_url,
                'country': home, 'storage': 'patterns'}
    output.mkdir(parents=True, exist_ok=True); database = output / 'NationalTimetable.sqlite'
    catalog = write_catalog(stations, variants, database, metadata)
    if catalog['stations'] < minimum_stations or catalog['variants'] < minimum_variants:
        raise ValueError(f'Coverage sanity gate failed: {catalog}')
    manifest = package(database, output, base_url, metadata)
    report = {'country': home, 'sourceCheckedAt': source_checked_at, 'generatedAt': now, 'feeds': [f.name for f in feeds],
              'trips': dict(counts), 'catalog': catalog, 'sha256': manifest['sha256'],
              'stationCountries': dict(collections.Counter(s['country'] for s in stations.values())),
              'excludedTrips': exclusions}
    (output / 'source-check.json').write_text(json.dumps(report, indent=2, ensure_ascii=False) + '\n')
    print(json.dumps({k: v for k, v in report.items() if k != 'excludedTrips'} | {'excludedTrips': len(exclusions)}, indent=2), flush=True)
    return manifest

if __name__ == '__main__':
    p = argparse.ArgumentParser()
    p.add_argument('feeds', nargs='*', type=pathlib.Path, help='GTFS zip files; default: the inventory\'s feeds')
    p.add_argument('--inventory', type=pathlib.Path, default=pathlib.Path('.cache/gtfs/de/inventory.json'),
                   help='download_gtfs.py provenance; supplies the feeds, country and source check')
    p.add_argument('--output', type=pathlib.Path, default=pathlib.Path('site/catalog/de/v2'))
    p.add_argument('--base-url', default='https://raw.githubusercontent.com/FBukovina/raily-timetables/catalog/catalog/de/v2')
    p.add_argument('--source-checked-at', help='Overrides the inventory, for feeds downloaded by hand')
    p.add_argument('--minimum-stations', type=int, default=4000); p.add_argument('--minimum-variants', type=int, default=50000)
    a = p.parse_args()
    inventory = json.loads(a.inventory.read_text()) if a.inventory.exists() else None
    if inventory and inventory.get('complete') is not True: raise ValueError('Refusing incomplete source inventory')
    feeds = a.feeds or [a.inventory.parent / name for name in sorted(inventory['feeds'])]
    checked = a.source_checked_at or (inventory and inventory['sourceCheckedAt'])
    if not checked: raise ValueError('No source check: pass --source-checked-at or an inventory')
    build(feeds, a.output, a.base_url, (inventory or {}).get('country', 'DE'), (inventory or {}).get('source', 'https://gtfs.de/en/feeds/'),
          checked, a.minimum_stations, a.minimum_variants)
