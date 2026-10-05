#!/usr/bin/env python3
"""CIS JŘ passenger railway messages -> validated, versioned SQLite catalog.

Official semantics: PA identity, latest CZPTTCreation, cancellation calendars and
explicit midnight Offset. No interpolation, realtime claims or location guessing.
"""
from __future__ import annotations
import argparse, collections, datetime as dt, gzip, hashlib, json, pathlib, re, shutil
import sqlite3, unicodedata, xml.etree.ElementTree as ET, zipfile
from download import xml_bytes, MAX_BYTES, BASE
SCHEMA = '''
PRAGMA user_version=1;
PRAGMA foreign_keys=ON;
CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL) WITHOUT ROWID;
CREATE TABLE stations(id TEXT PRIMARY KEY,name TEXT NOT NULL,normalized_name TEXT NOT NULL,country TEXT NOT NULL,code TEXT NOT NULL,latitude REAL,longitude REAL) WITHOUT ROWID;
CREATE TABLE variants(id TEXT PRIMARY KEY,train_number TEXT NOT NULL,title TEXT NOT NULL,operator_name TEXT NOT NULL,destination TEXT NOT NULL,facilities_json TEXT NOT NULL DEFAULT '[]') WITHOUT ROWID;
CREATE TABLE service_days(variant_id TEXT NOT NULL REFERENCES variants(id),day TEXT NOT NULL,PRIMARY KEY(variant_id,day)) WITHOUT ROWID;
CREATE TABLE calls(variant_id TEXT NOT NULL REFERENCES variants(id),sequence INTEGER NOT NULL,station_id TEXT NOT NULL REFERENCES stations(id),arrival_seconds INTEGER,arrival_day_offset INTEGER,departure_seconds INTEGER,departure_day_offset INTEGER,allows_boarding INTEGER NOT NULL CHECK(allows_boarding IN(0,1)),allows_alighting INTEGER NOT NULL CHECK(allows_alighting IN(0,1)),PRIMARY KEY(variant_id,sequence),CHECK(arrival_seconds IS NULL OR arrival_seconds BETWEEN 0 AND 86399),CHECK(departure_seconds IS NULL OR departure_seconds BETWEEN 0 AND 86399),CHECK(arrival_day_offset IS NULL OR arrival_day_offset BETWEEN -7 AND 7),CHECK(departure_day_offset IS NULL OR departure_day_offset BETWEEN -7 AND 7)) WITHOUT ROWID;
CREATE INDEX stations_name ON stations(normalized_name);
CREATE INDEX variants_number ON variants(train_number);
CREATE INDEX service_days_day ON service_days(day,variant_id);
CREATE INDEX calls_station ON calls(station_id,variant_id,sequence);
'''
SERVICE_CODES={10,11,12,13,14,17,34,18,24,25,19,20,21,22,26,27,28,29,36,30,40,41,33,38,39,42,46}

def normalized(value):
    return ''.join(c for c in unicodedata.normalize('NFKD',value.lower()) if not unicodedata.combining(c) and c.isalnum())

def required(node,path):
    value=node.findtext(path)
    if value is None or not value.strip(): raise ValueError(f'Missing {path}')
    return value.strip()

def identity(node):
    return ':'.join(required(node,k) for k in ('ObjectType','Company','Core','Variant','TimetableYear'))

def path_identity(root,related=False):
    tag='RelatedPlannedTransportIdentifiers' if related else 'PlannedTransportIdentifiers'
    matches=[n for n in root.iter(tag) if n.findtext('ObjectType')=='PA']
    if related: return [identity(n) for n in matches]
    if len(matches)!=1: raise ValueError('Expected exactly one PA identity')
    return identity(matches[0])

def parse_date(value):
    return dt.date.fromisoformat(value[:10])

def calendar(node,partial=False):
    if node is None: raise ValueError('Missing calendar')
    start=node.findtext('ValidityPeriod/StartDateTime') or node.findtext('StartDate')
    if not start: raise ValueError('Missing calendar start')
    start=parse_date(start)
    bits=node.findtext('BitmapDays')
    if not bits:
        if partial: return {start.isoformat()}
        raise ValueError('Missing calendar bitmap')
    end=parse_date(required(node,'ValidityPeriod/EndDateTime'))
    if len(bits)!=(end-start).days+1 or set(bits)-{'0','1'} or len(bits)>800:
        raise ValueError('Invalid bitmap length/content')
    return {(start+dt.timedelta(days=i)).isoformat() for i,v in enumerate(bits) if v=='1'}

def timestamp(value):
    # Source timestamps without zones are all KADR local civil time; source versions
    # are compared consistently. Numeric ISO components avoid textual timezone order.
    from zoneinfo import ZoneInfo
    value=dt.datetime.fromisoformat(value)
    if value.tzinfo is None: value=value.replace(tzinfo=ZoneInfo('Europe/Prague'))
    return value.timestamp()

def station(node):
    country=required(node,'CountryCodeISO'); code=required(node,'LocationPrimaryCode')
    if not re.fullmatch('[A-Z]{2}',country) or not re.fullmatch('[0-9]{1,5}',code): raise ValueError('Invalid station identity')
    code=code.zfill(5)
    return {'id':f'{country}:{code}','country':country,'code':code,'name':node.findtext('PrimaryLocationName') or f'{country} {code}'}

def parameters(node):
    result=collections.defaultdict(list)
    for n in node.findall('NetworkSpecificParameter'): result[required(n,'Name')].append(n.findtext('Value') or '')
    return result

def timing(node,qualifier):
    choices=[t for t in node.findall('TimingAtLocation/Timing') if t.get('TimingQualifierCode')==qualifier]
    if not choices: return None,None
    if len(choices)!=1: raise ValueError('Repeated timing qualifier')
    text=required(choices[0],'Time')
    # KADR encodes the timetable wall clock with a fixed +01:00 suffix even for
    # summer calendars. Offset is a separate count of local midnight crossings.
    match=re.fullmatch(r'(\d\d):(\d\d):(\d\d)(?:\.\d+)?(?:[+-]\d\d:\d\d|Z)?',text)
    if not match: raise ValueError(f'Invalid Time {text}')
    h,m,s=map(int,match.groups()); offset=int(required(choices[0],'Offset'))
    if h>23 or m>59 or s>59 or not -7<=offset<=7: raise ValueError('Timing out of bounds')
    return h*3600+m*60+s,offset

def parse_message(data,codebooks=None):
    if len(data)>MAX_BYTES or b'<!DOCTYPE' in data.upper(): raise ValueError('Unsafe XML')
    root=ET.fromstring(data)
    # Namespace qualified versions are equivalent; strip XML namespace only.
    for node in root.iter(): node.tag=node.tag.rsplit('}',1)[-1]
    key=path_identity(root)
    if root.tag=='CZCanceledPTTMessage':
        section=root.find('CZDeactivatedSection')
        endpoints=None
        if section is not None:
            endpoints=[]
            for name in ('StartLocation','EndLocation'):
                point=section.find(name)
                if point is None: raise ValueError('Missing cancellation endpoint')
                # LocationIdent/Location wrappers are used by TAF versions.
                if point.find('CountryCodeISO') is None:
                    point=next((n for n in point.iter() if n.find('CountryCodeISO') is not None),None)
                if point is None: raise ValueError('Missing cancellation location')
                endpoints.append(station(point)['id'])
        return {'kind':'cancel','id':key,'created':timestamp(required(root,'CZPTTCancelation')),'days':calendar(root.find('PlannedCalendar'),section is not None),'section':endpoints}
    if root.tag!='CZPTTCISMessage': raise ValueError(f'Unsupported XML root {root.tag}')
    info=root.find('CZPTTInformation')
    if info is None: raise ValueError('Missing timetable information')
    params=parameters(root); points=[]
    for location in info.findall('CZPTTLocation'):
        point=station(location.find('Location')); local=parameters(location)
        arrival,arrival_offset=timing(location,'ALA'); departure,departure_offset=timing(location,'ALD')
        activities={n.text for n in location.findall('TrainActivity/TrainActivityType')}
        passenger=bool(activities&{'0001','0028','0029','0030'}) or local.get('CZPassengerDwellTime')==['1']
        # 0028/0029 restrict 0001 even when both are supplied (the common case).
        boarding=passenger and '0029' not in activities and departure is not None
        alighting=passenger and '0028' not in activities and arrival is not None
        point.update(arrival_seconds=arrival,arrival_day_offset=arrival_offset,departure_seconds=departure,departure_day_offset=departure_offset,allows_boarding=int(boarding),allows_alighting=int(alighting),
                     public_rail=location.findtext('TrainType')=='1' and local.get('CZAlternativeTransport',['0'])[0]=='0',
                     number=local.get('CZReferenceTrainNumber',[location.findtext('OperationalTrainNumber') or ''])[0],
                     operator=location.findtext('ResponsibleRU') or '',category=location.findtext('CommercialTrafficType') or '',
                     inconsistent=local.get('CZInconsistentTime')==['1'],services=local.get('CZService',[]))
        points.append(point)
    if len(points)<2: raise ValueError('Fewer than two locations')
    facilities=[]
    note_calendar_parts={}
    for raw in params.get('CZCalendarPTTNote',[]):
        fields=raw.split('|')
        if len(fields)<4: continue
        note_calendar_parts[fields[0]]=fields
    def note_calendar(key):
        seen=set(); fragments=[]; current=key; start=None; end=None
        while current:
            if current in seen or current not in note_calendar_parts: return None
            seen.add(current); fields=note_calendar_parts[current]
            try:
                part_start=dt.datetime.strptime(fields[1],'%Y%m%d').date()
                part_end=dt.datetime.strptime(fields[2],'%Y%m%d').date()
            except ValueError: return None
            if start is None: start,end=part_start,part_end
            if (part_start,part_end)!=(start,end): return None
            fragments.append(fields[3]); current=fields[4] if len(fields)>4 else ''
        bits=''.join(fragments)
        if set(bits)-{'0','1'} or len(bits)!=(end-start).days+1: return None
        return {'start':start.isoformat(),'bitmap':bits}
    def note_point(code,occurrence):
        if len(code)<3: return None
        key=f'{code[:2]}:{code[2:].zfill(5)}'
        matches=[i for i,p in enumerate(points) if p['id']==key]
        try: return matches[int(occurrence or '0')]
        except (IndexError,ValueError): return None
    for note in params.get('CZCentralPTTNote',[]):
        fields=note.split('|')
        if len(fields)<7 or not fields[0].isdigit() or int(fields[0]) not in SERVICE_CODES: continue
        lo,hi=note_point(fields[1],fields[2]),note_point(fields[3],fields[4])
        if lo is None or hi is None or hi<lo: continue
        entry={'code':int(fields[0]),'sourceStart':lo,'sourceEnd':hi}
        if fields[6]:
            resolved=note_calendar(fields[6])
            if resolved is None: continue
            entry['calendar']=resolved
        if entry not in facilities: facilities.append(entry)
    original=None
    if params.get('CZReroute')==['1'] and params.get('CZOriginalCalendarStartDate') and params.get('CZOriginalCalendarBitmaps'):
        start=parse_date(params['CZOriginalCalendarStartDate'][0]); bits=''.join(params['CZOriginalCalendarBitmaps'])
        if set(bits)-{'0','1'} or len(bits)>800: raise ValueError('Invalid replacement calendar')
        original={(start+dt.timedelta(days=i)).isoformat() for i,c in enumerate(bits) if c=='1'}
    return {'kind':'path','id':key,'created':timestamp(required(root,'CZPTTCreation')),'days':calendar(info.find('PlannedCalendar')),'points':points,'name':params.get('CZTrainName',[''])[0],'facilities':facilities,'related':path_identity(root,True),'original':original}

class Timetables:
    def __init__(self): self.paths={}; self.cancellations=collections.defaultdict(list); self.counts=collections.Counter()
    def add(self,message):
        self.counts[message['kind']]+=1; key=message['id']
        if message['kind']=='cancel': self.cancellations[key].append(message); return
        previous=self.paths.get(key)
        if previous is None or message['created']>previous['created']: self.paths[key]=message
        elif message['created']==previous['created'] and message!=previous: raise ValueError(f'Conflicting same-time path {key}')
    def resolve(self):
        # A planned reroute carries original-calendar dates which may differ from
        # its own dates. Never cancel a related path using replacement run dates.
        replacements=collections.defaultdict(list)
        for path in self.paths.values():
            if path['original'] is not None:
                for related in path['related']: replacements[related].append(path)
        for key,path in sorted(self.paths.items()):
            days=set(path['days']); sections=collections.defaultdict(list)
            for c in self.cancellations[key]:
                if c['created']<path['created']: continue
                if c['section'] is None: days-=c['days']
                else:
                    for day in c['days']: sections[day].append(c['section'])
            for r in replacements[key]:
                if r['created']>=path['created']: days-=r['original']
            groups=collections.defaultdict(set)
            points=path['points']
            for day in sorted(days):
                lo,hi=0,len(points)-1
                for a,b in sections[day]:
                    matches=[(i,j) for i,p in enumerate(points) if p['id']==a for j,q in enumerate(points) if q['id']==b and j>i and (i==0 or j==len(points)-1)]
                    if len(matches)!=1: raise ValueError(f'Ambiguous/unsupported section cancellation {key} {day}')
                    i,j=matches[0]
                    if i==0: lo=max(lo,j)
                    if j==len(points)-1: hi=min(hi,i)
                # Only contiguous public railway segments are offered. Service and
                # replacement-bus links break a journey; no invented rail connection.
                start=None
                for index in range(lo,hi):
                    if points[index]['public_rail']:
                        if start is None: start=index
                    elif start is not None:
                        groups[(start,index)].add(day); start=None
                if start is not None: groups[(start,hi)].add(day)
            for (lo,hi),dates in sorted(groups.items()):
                calls=[dict(p,source_sequence=i) for i,p in enumerate(points) if lo<=i<=hi and (p['allows_boarding'] or p['allows_alighting'])]
                if len(calls)<2: continue
                calls[0]['arrival_seconds']=None; calls[0]['arrival_day_offset']=None; calls[0]['allows_alighting']=0
                calls[-1]['departure_seconds']=None; calls[-1]['departure_day_offset']=None; calls[-1]['allows_boarding']=0
                if not any(c['allows_boarding'] for c in calls[:-1]) or not any(c['allows_alighting'] for c in calls[1:]): continue
                times=[c[k+'_day_offset']*86400+c[k+'_seconds'] for c in calls for k in ('arrival','departure') if c[k+'_seconds'] is not None]
                if times!=sorted(times):
                    if any(c['inconsistent'] for c in calls):
                        self.counts['excludedCountertimeVariants']+=1
                        continue
                    raise ValueError(f'Unmarked decreasing call times: {key}')
                effective=key if (lo,hi)==(0,len(points)-1) else f'{key}:section:{lo}-{hi}'
                facilities=[]
                for facility in path['facilities']:
                    covered=[i for i,c in enumerate(calls) if facility['sourceStart']<=c['source_sequence']<=facility['sourceEnd']]
                    if not covered: continue
                    first,last=covered[0],covered[-1]
                    item={'code':facility['code'],'startSequence':first,'endSequence':last,'startStationName':calls[first]['name'],'endStationName':calls[last]['name']}
                    if 'calendar' in facility: item['calendar']=facility['calendar']
                    facilities.append(item)
                yield dict(path,id=effective,calls=calls,days=dates,facilities=facilities)

def read_sources(cache):
    manifest=json.loads((cache/'inventory.json').read_text())
    if manifest.get('complete') is not True: raise ValueError('Refusing incomplete source inventory')
    if 'years' in manifest:
        total=0
        for year in manifest['years']:
            child=cache/str(year)
            yield from read_sources(child)
            total+=json.loads((child/'inventory.json').read_text())['expectedAmendments']
        if total!=manifest['expectedAmendments']: raise ValueError('Incomplete annual set')
        return
    total=0
    annual=cache/f'JR{manifest["year"]}.zip'
    if annual.stat().st_size!=manifest['annual']['bytes']: raise ValueError('Annual archive size mismatch')
    if manifest['annual'].get('sha256') and hashlib.sha256(annual.read_bytes()).hexdigest()!=manifest['annual']['sha256']: raise ValueError('Annual archive checksum mismatch')
    with zipfile.ZipFile(annual) as archive:
        for name in archive.namelist():
            if name.lower().endswith('.xml'):
                if archive.getinfo(name).file_size>MAX_BYTES: raise ValueError('Oversize annual XML')
                yield f'annual/{name}',archive.read(name)
    for month in manifest['months']:
        db=sqlite3.connect(f'file:{cache/month["name"]}.sqlite?mode=ro',uri=True)
        inventory=[r[0] for r in db.execute('SELECT url FROM inventory ORDER BY url')]
        if len(inventory)!=month['count'] or hashlib.sha256('\n'.join(inventory).encode()).hexdigest()!=month['inventorySHA256']: raise ValueError('Inventory mismatch')
        count=0
        for url,data,digest in db.execute('SELECT files.url,data,sha256 FROM files JOIN inventory USING(url) ORDER BY files.url'):
            if hashlib.sha256(data).hexdigest()!=digest: raise ValueError('Cached amendment checksum mismatch')
            count+=1; yield url,xml_bytes(data)
        db.close()
        if count!=month['count']: raise ValueError('Missing listed amendments')
        total+=count
    if total!=manifest['expectedAmendments']: raise ValueError('Incomplete amendment set')

def load_codebooks():
    data=json.loads(pathlib.Path(__file__).with_name('codebooks.json').read_text())['tables']
    groups=collections.defaultdict(list)
    for row in data['SeznamSpolecnosti']:
        if row.get('EvCisloEU'): groups[row['EvCisloEU']].append(row['ObchodNazev'])
    # RICS may list multiple legal branches. Preserve the code rather than guess
    # which company/branch operated the particular journey.
    operators={k:next(iter(set(v))) for k,v in groups.items() if len(set(v))==1}
    categories={row['KodTAF']:row['Kod'] for row in data['SeznamKomercniDruhVlaku']}
    return operators,categories

def write_catalog(paths,output,metadata,operators=None,categories=None):
    operators=operators or {}; categories=categories or {}; temp=output.with_suffix('.partial')
    temp.unlink(missing_ok=True); db=sqlite3.connect(temp); db.executescript(SCHEMA)
    db.executemany('INSERT INTO metadata VALUES(?,?)',((k,str(v)) for k,v in metadata.items()))
    counts=collections.Counter()
    for path in paths:
        calls=path['calls']; first=calls[0]; number=first['number']; category=categories.get(first['category'],'')
        if not number: raise ValueError(f'No passenger train number {path["id"]}')
        title=' '.join(p for p in (category,number,path['name']) if p)
        codes=list(dict.fromkeys(c['operator'] for c in calls if c['operator']))
        operator=' / '.join(operators.get(c,f'RICS {c}') for c in codes)
        db.execute('INSERT INTO variants VALUES(?,?,?,?,?,?)',(path['id'],number,title,operator,calls[-1]['name'],json.dumps(path['facilities'],ensure_ascii=False,separators=(',',':'))))
        db.executemany('INSERT INTO service_days VALUES(?,?)',((path['id'],d) for d in sorted(path['days'])))
        for sequence,p in enumerate(calls):
            db.execute('INSERT OR IGNORE INTO stations VALUES(?,?,?,?,?,?,?)',(p['id'],p['name'],normalized(p['name']),p['country'],p['code'],None,None))
            db.execute('INSERT INTO calls VALUES(?,?,?,?,?,?,?,?,?)',(path['id'],sequence,p['id'],p['arrival_seconds'],p['arrival_day_offset'],p['departure_seconds'],p['departure_day_offset'],p['allows_boarding'],p['allows_alighting']))
        counts['variants']+=1; counts['serviceDays']+=len(path['days']); counts['calls']+=len(calls)
    counts['stations']=db.execute('SELECT count(*) FROM stations').fetchone()[0]
    if not counts['variants']: raise ValueError('Empty catalog')
    if db.execute('PRAGMA integrity_check').fetchone()[0]!='ok' or db.execute('PRAGMA foreign_key_check').fetchall(): raise ValueError('SQLite validation failed')
    db.commit(); db.execute('VACUUM'); db.close(); temp.replace(output)
    return dict(counts)

def build(cache,output,base_url):
    inventory=json.loads((cache/'inventory.json').read_text())
    if inventory.get('complete') is not True: raise ValueError('Refusing incomplete source inventory')
    store=Timetables()
    for index,(source,data) in enumerate(read_sources(cache)):
        try: store.add(parse_message(data))
        except Exception as e: raise ValueError(f'{source}: {e}') from e
        if index%10000==0: print(f'Parsed {index} messages',flush=True)
    paths=list(store.resolve()); dates=[d for p in paths for d in p['days']]
    now=dt.datetime.now(dt.timezone.utc).isoformat(timespec='seconds').replace('+00:00','Z')
    metadata={'schemaVersion':1,'validFrom':min(dates),'validThrough':max(dates),'sourceCheckedAt':inventory['sourceCheckedAt'],'generatedAt':now,'sourceURL':BASE}
    output.mkdir(parents=True,exist_ok=True); database=output/'NationalTimetable.sqlite'
    counts=write_catalog(paths,database,metadata,*load_codebooks())
    if counts['stations']<2000 or counts['variants']<5000: raise ValueError('Coverage sanity gate failed')
    packed=gzip.compress(database.read_bytes(),compresslevel=9,mtime=0); digest=hashlib.sha256(packed).hexdigest(); filename=f'catalog-{digest}.sqlite.gz'
    (output/filename).write_bytes(packed)
    manifest={**metadata,'catalogURL':f'{base_url.rstrip("/")}/{filename}','compressedBytes':len(packed),'uncompressedBytes':database.stat().st_size,'sha256':digest}
    # Manifest replacement is last: a failure can never advertise an incomplete file.
    previous=output/'manifest.json'
    if previous.exists(): shutil.copy2(previous,output/'previous-manifest.json')
    (output/'manifest.partial').write_text(json.dumps(manifest,indent=2)+'\n'); (output/'manifest.partial').replace(previous)
    inputs=[json.loads((cache/str(y)/'inventory.json').read_text()) for y in inventory['years']] if 'years' in inventory else [inventory]
    for item in inputs:
        annual_path=cache/str(item['year'])/f'JR{item["year"]}.zip' if 'years' in inventory else cache/f'JR{item["year"]}.zip'
        item['annual']['sha256']=hashlib.sha256(annual_path.read_bytes()).hexdigest()
    report={'sourceCheckedAt':inventory['sourceCheckedAt'],'generatedAt':now,'years':[i['year'] for i in inputs],'amendments':inventory['expectedAmendments'],'messages':dict(store.counts),'catalog':counts,'sha256':digest,'sources':inputs}
    (output/'source-check.json').write_text(json.dumps(report,indent=2)+'\n')
    print(json.dumps(report,indent=2),flush=True)
    return manifest

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--cache',type=pathlib.Path,default=pathlib.Path('.cache/cisjr'));p.add_argument('--output',type=pathlib.Path,default=pathlib.Path('site/catalog/v1'));p.add_argument('--base-url',default='https://fbukovina.github.io/raily-timetables/catalog/v1')
    a=p.parse_args();build(a.cache,a.output,a.base_url)
