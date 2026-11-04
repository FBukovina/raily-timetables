import io, json, pathlib, sqlite3, sys, tempfile, unittest, zipfile
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / 'scripts'))
import gtfs_catalog
from gtfs_catalog import Borders, display_name, normalized, read_feeds, write_catalog

def feed(path, stop_times, calendar_dates='', trips=None):
    files = {
        'agency.txt': 'agency_id,agency_name,agency_url,agency_timezone\n1,DB Regio,https://bahn.de,Europe/Berlin\n',
        'routes.txt': 'route_id,agency_id,route_short_name,route_type\nr1,1,RE 1,2\nbus,1,100,3\n',
        'stops.txt': 'stop_id,stop_name,stop_lat,stop_lon,location_type,parent_station\n'
                     'B,"S+U Berlin Hauptbahnhof",52.525,13.369,1,\nB1,Berlin Hbf Gleis 1,52.525,13.369,,B\n'
                     'S,"Berlin, Spandau",52.534,13.197,,\nH,"Hamburg, Hamburg Hbf",53.553,10.007,,\n'
                     'P,Praha hl.n.,50.083,14.436,,\nG,Gießen Bahnhof,50.579,8.663,,\n',
        'trips.txt': trips or 'route_id,service_id,trip_id\nr1,wd,t1\nr1,wd,t2\nr1,we,t3\nr1,wd,t4\nbus,wd,b1\n',
        'calendar.txt': 'service_id,monday,tuesday,wednesday,thursday,friday,saturday,sunday,start_date,end_date\n'
                        'wd,1,1,1,1,1,0,0,20261102,20261108\nwe,0,0,0,0,0,1,1,20261102,20261108\n',
        'calendar_dates.txt': 'service_id,exception_type,date\n' + calendar_dates,
        'stop_times.txt': 'trip_id,arrival_time,departure_time,stop_id,stop_sequence,pickup_type,drop_off_type\n' + stop_times,
    }
    with zipfile.ZipFile(path, 'w') as archive:
        for name, text in files.items(): archive.writestr(name, text)
    return path

TIMES = ('t1,08:00:00,08:00:00,B1,1,,\nt1,08:15:00,08:16:00,S,2,,\nt1,09:45:00,09:45:00,H,3,,\n'
         't2,09:00:00,09:00:00,B1,1,,\nt2,09:15:00,09:16:00,S,2,,\nt2,10:45:00,10:45:00,H,3,,\n'
         't3,08:00:00,08:00:00,B1,1,,\nt3,08:15:00,08:16:00,S,2,,\nt3,09:45:00,09:45:00,H,3,,\n'
         't4,23:50:00,23:50:00,B1,1,,\nt4,24:10:00,24:11:00,G,2,,1\nt4,26:30:00,26:30:00,P,3,,\n'
         'b1,08:00:00,08:00:00,B1,1,,\nb1,08:30:00,08:30:00,S,2,,\n')

class GTFSCatalogTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.root = pathlib.Path(self.temp.name)

    def tearDown(self): self.temp.cleanup()

    def build(self, stop_times=TIMES, calendar_dates='wd,2,20261104\nwd,1,20261107\n'):
        stations, variants, exclusions, counts = read_feeds([feed(self.root / 'feed.zip', stop_times, calendar_dates)], 'DE', Borders())
        database = self.root / 'catalog.sqlite'
        write_catalog(stations, variants, database, {'schemaVersion': 2, 'validFrom': '2026-11-02', 'validThrough': '2026-11-08'})
        return sqlite3.connect(database), variants, exclusions, counts

    def test_display_names_keep_source_aliases(self):
        self.assertEqual(display_name('S+U Berlin Hauptbahnhof'), 'Berlin Hbf')
        self.assertEqual(display_name('Hamburg, Hamburg Hbf'), 'Hamburg Hbf')
        self.assertEqual(display_name('Dülmen, Bahnhof'), 'Dülmen')
        self.assertEqual(display_name('Kassel Bahnhof Wilhelmshöhe'), 'Kassel Wilhelmshöhe')
        self.assertEqual(display_name('Südkreuz Bhf (Berlin)'), 'Berlin Südkreuz')
        self.assertEqual(display_name('Berlin Albrechtshof Bhf'), 'Berlin Albrechtshof')
        self.assertEqual(display_name('Frankfurt (Oder)'), 'Frankfurt (Oder)')
        self.assertEqual(normalized('Gießen'), 'giessen')
        self.assertEqual(gtfs_catalog.operator_name('800153 DB Regio AG Nordost'), 'DB Regio AG Nordost')
        self.assertEqual(gtfs_catalog.operator_name('NBRB63 NEB Niederbarnimer Eisenbahn'), 'NEB Niederbarnimer Eisenbahn')
        self.assertEqual(gtfs_catalog.operator_name('PKP Intercity'), 'PKP Intercity')
        self.assertEqual(gtfs_catalog.operator_name('ODEG GmbH'), 'ODEG GmbH')

    def test_rail_only_patterns_calendars_and_identity(self):
        db, variants, exclusions, counts = self.build()
        # The bus route is ignored; t1 and t3 share one pattern and start, so one variant.
        self.assertEqual(db.execute('SELECT count(*) FROM variants').fetchone()[0], 3)
        self.assertEqual(db.execute('SELECT count(DISTINCT pattern_id) FROM variants').fetchone()[0], 2)
        self.assertEqual(counts['mergedTrips'], 1)
        merged = db.execute("SELECT d.days,d.first_day,d.last_day FROM variants v JOIN variant_days d ON d.variant_id=v.id WHERE v.start_seconds=28800").fetchone()
        # Weekdays minus the removed 4th, plus the added 7th, merged with the weekend service.
        self.assertEqual(merged, ('1101111', '2026-11-02', '2026-11-08'))
        names = [r[0] for r in db.execute('SELECT name FROM stations ORDER BY name')]
        self.assertEqual(names, ['Berlin Hbf', 'Berlin Spandau', 'Gießen', 'Hamburg Hbf', 'Praha hl.n.'])
        self.assertEqual(db.execute("SELECT country FROM stations WHERE name='Praha hl.n.'").fetchone()[0], 'CZ')
        self.assertIn('berlinhauptbahnhof', db.execute("SELECT normalized_name FROM stations WHERE id='B'").fetchone()[0])
        self.assertEqual(exclusions, [])

    def test_calls_view_splits_elapsed_times_into_civil_offsets(self):
        db, *_ = self.build()
        rows = db.execute('''SELECT c.station_id,c.arrival_seconds,c.arrival_day_offset,c.departure_seconds,c.departure_day_offset,
            c.allows_boarding,c.allows_alighting FROM calls c JOIN variants v ON v.id=c.variant_id WHERE v.start_seconds=85800 ORDER BY c.sequence''').fetchall()
        self.assertEqual(rows, [('B', None, None, 85800, 0, 1, 0), ('G', 600, 1, 660, 1, 1, 0), ('P', 9000, 1, None, None, 0, 1)])
        self.assertEqual(db.execute("SELECT destination FROM variants WHERE start_seconds=85800").fetchone()[0], 'Praha hl.n.')

    def test_identity_survives_renumbered_trips(self):
        _, first, *_ = self.build()
        renumbered = TIMES.replace('t1,', 'x9,').replace('t2,', 'x8,')
        trips = 'route_id,service_id,trip_id\nr1,wd,x9\nr1,wd,x8\nr1,we,t3\nr1,wd,t4\n'
        stations, second, *_ = read_feeds([feed(self.root / 'next.zip', renumbered, trips=trips)], 'DE', Borders())
        self.assertEqual(set(first), set(second))

    def test_backwards_times_are_quarantined_not_repaired(self):
        times = TIMES.replace('t2,10:45:00,10:45:00,H,3', 't2,09:10:00,09:10:00,H,3')
        db, variants, exclusions, counts = self.build(times)
        self.assertEqual(exclusions, [{'feed': 'feed.zip', 'tripID': 't2', 'reason': 'times run backwards'}])
        self.assertEqual(db.execute('SELECT count(*) FROM variants').fetchone()[0], 2)

if __name__ == '__main__': unittest.main()
