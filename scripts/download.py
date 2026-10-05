#!/usr/bin/env python3
"""Fetch every CIS annual archive and amendment into resumable monthly SQLite caches."""
import argparse, concurrent.futures, datetime as dt, gzip, hashlib, html.parser, http.client
import json, pathlib, re, sqlite3, threading, time, urllib.parse, urllib.request, zipfile, io, subprocess
BASE = 'https://portal.cisjr.cz/pub/draha/celostatni/szdc/'
MAX_BYTES = 8 * 1024 * 1024
class Links(html.parser.HTMLParser):
    def __init__(self): super().__init__(); self.links=[]
    def handle_starttag(self, tag, attrs):
        if tag.lower()=='a': self.links.extend(v for k,v in attrs if k.lower()=='href')
def request(url, limit=40*1024*1024):
    for attempt in range(6):
        try:
            with urllib.request.urlopen(url, timeout=90) as r:
                data=r.read(limit+1); headers=dict(r.headers)
            if len(data)>limit: raise ValueError('Oversize source index')
            return data,headers
        except Exception:
            if attempt==5: raise
            time.sleep(min(2**attempt,30))
def index(url):
    data,_=request(url)
    parser=Links(); parser.feed(data.decode('utf-8'))
    return sorted(set(urllib.parse.urljoin(url, p) for p in parser.links))
def xml_bytes(data):
    if data.startswith(b'\x1f\x8b'):
        with gzip.GzipFile(fileobj=io.BytesIO(data)) as stream: result=stream.read(MAX_BYTES+1)
    elif data.startswith(b'PK'):
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            names=[n for n in archive.namelist() if n.lower().endswith('.xml')]
            if len(names)!=1: raise ValueError('Expected one XML in amendment')
            if archive.getinfo(names[0]).file_size>MAX_BYTES: raise ValueError('Oversize XML')
            result=archive.read(names[0])
    else: raise ValueError('Unknown amendment compression')
    if len(result)>MAX_BYTES or b'<!DOCTYPE' in result.upper(): raise ValueError('Unsafe XML')
    # Parsing happens at ingestion; reject obvious server error pages here.
    if not result.lstrip().startswith(b'<?xml') and not result.lstrip().startswith(b'<CZ'): raise ValueError('Not XML')
    return result
local=threading.local()
def fetch(item):
    url,modified,etag=item
    u=urllib.parse.urlsplit(url)
    for attempt in range(6):
        try:
            if not hasattr(local,'connection'): local.connection=http.client.HTTPSConnection(u.netloc,timeout=60)
            headers={'User-Agent':'RailyTimetables/1.0 (+https://github.com/FBukovina/raily-timetables)'}
            if etag: headers['If-None-Match']=etag
            elif modified: headers['If-Modified-Since']=modified
            local.connection.request('GET',u.path,headers=headers)
            response=local.connection.getresponse(); data=response.read(MAX_BYTES+1)
            if response.status==304: return url,None,modified,etag
            if response.status!=200 or len(data)>MAX_BYTES: raise ValueError(f'HTTP {response.status} or oversize response')
            xml_bytes(data)
            return url,data,response.getheader('Last-Modified'),response.getheader('ETag')
        except Exception:
            if hasattr(local,'connection'): local.connection.close(); del local.connection
            if attempt==5: raise
            time.sleep(min(2**attempt,30))
def run(cache,year):
    cache.mkdir(parents=True,exist_ok=True); root=f'{BASE}{year}/'
    urls=index(root); annual=f'{root}JR{year}.zip'
    if annual not in urls: raise ValueError(f'Annual archive absent: {annual}')
    # Always validate annual source metadata; a revised annual archive invalidates its cache.
    _,headers=request(urllib.request.Request(annual,method='HEAD'))
    annual_meta={'url':annual,'bytes':int(headers['Content-Length']),'lastModified':headers.get('Last-Modified')}
    annual_path=cache/f'JR{year}.zip'; meta_path=cache/f'JR{year}.json'
    if not (annual_path.exists() and meta_path.exists() and all(json.loads(meta_path.read_text()).get(k)==v for k,v in annual_meta.items()) and annual_path.stat().st_size==annual_meta['bytes']):
        print(f'Downloading annual archive {annual_meta["bytes"]} bytes',flush=True)
        temp=annual_path.with_suffix('.partial'); partial_meta=cache/f'JR{year}.partial.json'
        if not partial_meta.exists() or json.loads(partial_meta.read_text())!=annual_meta:
            temp.unlink(missing_ok=True)
        partial_meta.write_text(json.dumps(annual_meta,sort_keys=True))
        subprocess.run(['curl','--fail','--location','--retry','6','--retry-all-errors','--connect-timeout','30','--max-time','600','--continue-at','-','--output',str(temp),annual],check=True)
        if temp.stat().st_size!=annual_meta['bytes']: raise ValueError('Incomplete annual archive')
        with zipfile.ZipFile(temp) as z:
            if z.testzip(): raise ValueError('Corrupt annual archive')
        temp.replace(annual_path); partial_meta.unlink(missing_ok=True); meta_path.write_text(json.dumps(annual_meta,sort_keys=True))
    annual_meta['sha256']=hashlib.sha256(annual_path.read_bytes()).hexdigest()
    annual_meta['checkedAt']=dt.datetime.now(dt.timezone.utc).isoformat().replace('+00:00','Z')
    meta_path.write_text(json.dumps(annual_meta,sort_keys=True))
    months=[u for u in urls if re.search(r'/\d{4}-\d{2}/$',u)]
    inventories=[]
    for month in months:
        sources=[u for u in index(month) if u.endswith('.xml.zip')]
        if not sources: raise ValueError(f'Empty monthly inventory {month}')
        inventories.append((month,sources))
    observed=dt.datetime.now(dt.timezone.utc).isoformat().replace('+00:00','Z')
    expected=sum(len(s) for _,s in inventories)
    print(f'{len(months)} monthly inventories, {expected} amendments',flush=True)
    manifest={'year':year,'annual':annual_meta,'months':[],'expectedAmendments':expected,'complete':False,'sourceCheckedAt':observed}
    (cache/'inventory.json').write_text(json.dumps(manifest,indent=2))
    for month,sources in inventories:
        name=month.rstrip('/').rsplit('/',1)[-1]; path=cache/f'{name}.sqlite'
        db=sqlite3.connect(path); db.execute('PRAGMA journal_mode=WAL'); db.execute('CREATE TABLE IF NOT EXISTS files(url TEXT PRIMARY KEY, data BLOB NOT NULL, sha256 TEXT NOT NULL)')
        columns={r[1] for r in db.execute('PRAGMA table_info(files)')}
        for column in ('last_modified','etag','checked_at'):
            if column not in columns: db.execute(f'ALTER TABLE files ADD COLUMN {column} TEXT')
        known={r[0]:r[1:] for r in db.execute('SELECT url,last_modified,etag,checked_at FROM files')}
        # Recent successful downloads survive interrupted imports. Older cached
        # objects are conditionally revalidated because a PA URL can be revised.
        cutoff=(dt.datetime.now(dt.timezone.utc)-dt.timedelta(hours=6)).isoformat()
        needed=[(u,known.get(u,(None,None,None))[0],known.get(u,(None,None,None))[1]) for u in sources if u not in known or not known[u][2] or known[u][2]<cutoff]
        print(f'{name}: {len(sources)} files, {len(needed)} missing',flush=True)
        done=0
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            # map maintains a small submission window on older Python using explicit chunks.
            for offset in range(0,len(needed),500):
                for url,data,modified,etag in pool.map(fetch,needed[offset:offset+500]):
                    checked=dt.datetime.now(dt.timezone.utc).isoformat()
                    if data is None:
                        db.execute('UPDATE files SET checked_at=? WHERE url=?',(checked,url))
                    else:
                        db.execute('INSERT OR REPLACE INTO files(url,data,sha256,last_modified,etag,checked_at) VALUES(?,?,?,?,?,?)',(url,data,hashlib.sha256(data).hexdigest(),modified,etag,checked))
                    done+=1
                    if done%100==0: db.commit()
                db.commit(); print(f'{name}: downloaded {done}/{len(needed)}',flush=True)
        inventory_hash=hashlib.sha256('\n'.join(sources).encode()).hexdigest()
        # Only currently listed files are authoritative; obsolete cached URLs stay off the ingestion path.
        db.execute('CREATE TABLE IF NOT EXISTS inventory(url TEXT PRIMARY KEY)'); db.execute('DELETE FROM inventory')
        db.executemany('INSERT INTO inventory VALUES(?)',((s,) for s in sources)); db.commit(); db.execute('PRAGMA wal_checkpoint(TRUNCATE)'); db.close()
        manifest['months'].append({'name':name,'count':len(sources),'inventorySHA256':inventory_hash})
        (cache/'inventory.json').write_text(json.dumps(manifest,indent=2))
    manifest.update(complete=True)
    (cache/'inventory.json').write_text(json.dumps(manifest,indent=2))
    print(f'COMPLETE: {expected} amendments',flush=True)
def automatic(cache):
    # Include an announced upcoming timetable without dropping the current year.
    # Source keeps current/upcoming annual directories; expired calendar years are
    # irrelevant to new journey searches and saved journeys retain their snapshots.
    from zoneinfo import ZoneInfo
    year=dt.datetime.now(ZoneInfo('Europe/Prague')).year
    available=[]
    for url in index(BASE):
        match=re.search(r'/(\d{4})/$',url)
        if match and year<=int(match.group(1))<=year+1:
            candidate=int(match.group(1))
            if f'{url}JR{candidate}.zip' in index(url): available.append(candidate)
    if not available: raise ValueError('No current/upcoming annual archives published')
    cache.mkdir(parents=True,exist_ok=True)
    combined={'years':sorted(available),'complete':False}
    (cache/'inventory.json').write_text(json.dumps(combined,indent=2))
    reports=[]
    for candidate in sorted(available):
        run(cache/str(candidate),candidate)
        reports.append(json.loads((cache/str(candidate)/'inventory.json').read_text()))
    combined.update(complete=True,sourceCheckedAt=min(r['sourceCheckedAt'] for r in reports),expectedAmendments=sum(r['expectedAmendments'] for r in reports))
    (cache/'inventory.json').write_text(json.dumps(combined,indent=2))

if __name__=='__main__':
    p=argparse.ArgumentParser(); p.add_argument('--cache',type=pathlib.Path,default=pathlib.Path('.cache/cisjr')); p.add_argument('--year',type=int)
    args=p.parse_args()
    if args.year: run(args.cache,args.year)
    else: automatic(args.cache)
