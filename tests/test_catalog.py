import datetime as dt
import gzip
import hashlib
import json
import pathlib
import sqlite3
import sys
import tempfile
import unittest
sys.path.insert(0,str(pathlib.Path(__file__).resolve().parents[1]/'scripts'))
from catalog import Timetables,parse_message,write_catalog,calendar,normalized,read_sources
from download import xml_bytes

# Hand-authored deterministic XML, based on the public v1.09.06 schema.
# These fictional trains and locations must never ship as production data.
def ident(core='ORIGINAL',related=False):
    tag='RelatedPlannedTransportIdentifiers' if related else 'PlannedTransportIdentifiers'
    return f'<{tag}><ObjectType>PA</ObjectType><Company>0054</Company><Core>{core}</Core><Variant>00</Variant><TimetableYear>2026</TimetableYear></{tag}>'
def cal(start='2026-10-01',bits='111'):
    end=(dt.date.fromisoformat(start)+dt.timedelta(days=len(bits)-1)).isoformat()
    return f'<PlannedCalendar><BitmapDays>{bits}</BitmapDays><ValidityPeriod><StartDateTime>{start}T00:00:00</StartDateTime><EndDateTime>{end}T00:00:00</EndDateTime></ValidityPeriod></PlannedCalendar>'
def location(code):
    return f'<CountryCodeISO>CZ</CountryCodeISO><LocationPrimaryCode>{code}</LocationPrimaryCode><PrimaryLocationName>Stop {code}</PrimaryLocationName>'
def point(code,hour='12:00:00.0000000+01:00',offset=0,activities=('0001',),alternative=False,train_type='1'):
    times=''.join(f'<Timing TimingQualifierCode="{q}"><Time>{hour}</Time><Offset>{offset}</Offset></Timing>' for q in ('ALA','ALD'))
    acts=''.join(f'<TrainActivity><TrainActivityType>{a}</TrainActivityType></TrainActivity>' for a in activities)
    extra='<NetworkSpecificParameter><Name>CZAlternativeTransport</Name><Value>1</Value></NetworkSpecificParameter>' if alternative else ''
    return f'<CZPTTLocation><Location>{location(code)}</Location><TimingAtLocation>{times}</TimingAtLocation><TrainType>{train_type}</TrainType><OperationalTrainNumber>123</OperationalTrainNumber><ResponsibleRU>1154</ResponsibleRU>{acts}{extra}</CZPTTLocation>'
def path(core='ORIGINAL',created='2026-09-01T10:00:00',bits='111',points=None,start='2026-10-01',extra='',related=''):
    points=points or [point('10001'),point('10002'),point('10003'),point('10004')]
    return (f'<CZPTTCISMessage><Identifiers>{ident(core)}{ident(related,True) if related else ""}</Identifiers><CZPTTCreation>{created}</CZPTTCreation><CZPTTInformation>{"".join(points)}{cal(start,bits)}</CZPTTInformation>{extra}</CZPTTCISMessage>').encode()
def cancel(bits='010',created='2026-09-02T10:00:00',core='ORIGINAL',section=None,start='2026-10-01'):
    sect=f'<CZDeactivatedSection><StartLocation>{location(section[0])}</StartLocation><EndLocation>{location(section[1])}</EndLocation></CZDeactivatedSection>' if section else ''
    return f'<CZCanceledPTTMessage>{ident(core)}<CZPTTCancelation>{created}</CZPTTCancelation>{cal(start,bits)}{sect}</CZCanceledPTTMessage>'.encode()
def parameter(key,value): return f'<NetworkSpecificParameter><Name>{key}</Name><Value>{value}</Value></NetworkSpecificParameter>'
def resolve(*messages):
    store=Timetables()
    for xml in messages: store.add(parse_message(xml))
    return list(store.resolve())

class CatalogTests(unittest.TestCase):
    def test_calendar_exceptions_are_operating_dates(self):
        self.assertEqual(resolve(path(bits='101'))[0]['days'],{'2026-10-01','2026-10-03'})
    def test_cancellation_order_does_not_depend_on_filenames(self):
        a=resolve(cancel(),path());b=resolve(path(),cancel())
        self.assertEqual(a,b); self.assertEqual(a[0]['days'],{'2026-10-01','2026-10-03'})
    def test_newest_path_replaces_older_same_pa_snapshot(self):
        result=resolve(path(bits='111'),cancel(),path(created='2026-09-03T10:00:00',bits='010'))
        self.assertEqual(result[0]['days'],{'2026-10-02'})
    def test_same_number_distinct_pa_is_not_merged(self):
        self.assertEqual(len(resolve(path(),path(core='OTHER'))),2)
    def test_replacement_uses_original_not_shifted_run_calendar(self):
        extra=''.join(parameter(k,v) for k,v in [('CZReroute','1'),('CZOriginalCalendarStartDate','2026-10-02'),('CZOriginalCalendarBitmaps','1')])
        result=resolve(path(),path(core='NEW',created='2026-09-04T10:00:00',start='2026-10-01',bits='1',related='ORIGINAL',extra=extra))
        old=next(p for p in result if 'ORIGINAL' in p['id']);new=next(p for p in result if 'NEW' in p['id'])
        self.assertNotIn('2026-10-02',old['days']);self.assertIn('2026-10-01',old['days']);self.assertEqual(new['days'],{'2026-10-01'})
    def test_both_end_section_cancellations_preserve_boundary_stops(self):
        result=resolve(path(),cancel(section=('10001','10002')),cancel(section=('10003','10004')))
        partial=next(p for p in result if p['days']=={'2026-10-02'})
        self.assertEqual([p['code'] for p in partial['calls']],['10002','10003'])
        self.assertFalse(partial['calls'][0]['allows_alighting']);self.assertFalse(partial['calls'][-1]['allows_boarding'])
    def test_ambiguous_repeated_cancellation_endpoint_fails_closed(self):
        points=[point('10001'),point('10002'),point('10002'),point('10004')]
        with self.assertRaisesRegex(ValueError,'Ambiguous'): resolve(path(points=points),cancel(section=('10001','10002')))
    def test_repeated_passenger_stations_keep_distinct_calls(self):
        result=resolve(path(points=[point('10001'),point('10002'),point('10001')]))[0]
        self.assertEqual([p['code'] for p in result['calls']],['10001','10002','10001'])
    def test_boarding_alighting_restrictions_override_general_stop(self):
        points=[point('10001'),point('10002',activities=('0001','0028')),point('10003',activities=('0001','0029')),point('10004')]
        calls=resolve(path(points=points))[0]['calls']
        self.assertTrue(calls[1]['allows_boarding']);self.assertFalse(calls[1]['allows_alighting'])
        self.assertFalse(calls[2]['allows_boarding']);self.assertTrue(calls[2]['allows_alighting'])
    def test_midnight_offsets_preserved_without_timezone_or_dst_conversion(self):
        calls=resolve(path(points=[point('10001','23:59:00+01:00',-1),point('10002','02:30:00+01:00',0)],start='2026-10-25',bits='1'))[0]['calls']
        self.assertEqual((calls[0]['departure_seconds'],calls[0]['departure_day_offset']),(86340,-1))
        self.assertEqual((calls[1]['arrival_seconds'],calls[1]['arrival_day_offset']),(9000,0))
    def test_bus_and_nonpublic_sections_cannot_connect_passengers(self):
        points=[point('10001'),point('10002',alternative=True),point('10003'),point('10004')]
        result=resolve(path(points=points))
        self.assertEqual([[c['code'] for c in p['calls']] for p in result],[['10001','10002'],['10003','10004']])
        self.assertEqual(resolve(path(points=[point('10001',train_type='2'),point('10002',train_type='2')])),[])
    def test_facility_calendar_and_repeated_section_occurrence(self):
        extra=parameter('CZCalendarPTTNote','9|20261001|20261003|101|')+parameter('CZCentralPTTNote','40|CZ10001|1|CZ10004||0|9')
        p=resolve(path(points=[point('10001'),point('10002'),point('10001'),point('10004')],extra=extra))[0]
        self.assertEqual(p['facilities'][0]['startSequence'],2)
        self.assertEqual(p['facilities'][0]['calendar'],{'start':'2026-10-01','bitmap':'101'})
    def test_unknown_facility_calendar_is_not_assumed_daily(self):
        p=resolve(path(extra=parameter('CZCentralPTTNote','40|CZ10001||CZ10004||0|missing')))[0]
        self.assertEqual(p['facilities'],[])
    def test_normalized_search_matches_app_punctuation_and_diacritics(self):
        self.assertEqual(normalized('České Budějovice hl.n.'),'ceskebudejovicehln')
    def test_unmarked_decreasing_times_fail_closed(self):
        with self.assertRaisesRegex(ValueError,'Unmarked decreasing'):
            resolve(path(points=[point('10001','12:10:00'),point('10002','12:00:00')]))
    def test_declared_countertime_is_not_published_as_normal_schedule(self):
        middle=point('10002','12:00:00').replace('</CZPTTLocation>',parameter('CZInconsistentTime','1')+'</CZPTTLocation>')
        self.assertEqual(resolve(path(points=[point('10001','12:10:00'),middle,point('10003','12:30:00')])),[])
    def test_gzip_inflation_is_bounded(self):
        with self.assertRaisesRegex(ValueError,'Unsafe XML'):
            xml_bytes(gzip.compress(b'X'*(8*1024*1024+1)))
    def test_sqlite_foreign_keys_and_indices(self):
        with tempfile.TemporaryDirectory() as folder:
            output=pathlib.Path(folder)/'catalog.sqlite';counts=write_catalog(resolve(path()),output,{'validFrom':'2026-10-01'})
            db=sqlite3.connect(output)
            self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0],1)
            self.assertEqual(db.execute('PRAGMA foreign_key_check').fetchall(),[])
            self.assertEqual(counts['calls'],4);self.assertEqual(db.execute('SELECT count(*) FROM service_days').fetchone()[0],3)
    def test_incomplete_inventory_cannot_be_published(self):
        with tempfile.TemporaryDirectory() as folder:
            root=pathlib.Path(folder);(root/'inventory.json').write_text('{"complete":false}')
            with self.assertRaisesRegex(ValueError,'incomplete'): list(read_sources(root))
    def test_gzip_crc_failure_is_rejected(self):
        packed=bytearray(gzip.compress(path()));packed[-8]^=1
        with self.assertRaises(Exception): xml_bytes(bytes(packed))
    def test_xml_external_entities_rejected(self):
        with self.assertRaisesRegex(ValueError,'Unsafe'): parse_message(b'<!DOCTYPE foo><CZPTTCISMessage/>')

if __name__=='__main__': unittest.main()
