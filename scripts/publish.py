#!/usr/bin/env python3
"""Validate Pages artifacts and preserve the preceding immutable catalog."""
import argparse, datetime as dt, gzip, hashlib, io, json, lzma, pathlib, shutil, sqlite3, tempfile
import urllib.error, urllib.parse, urllib.request
ROOT='https://raw.githubusercontent.com/FBukovina/raily-timetables/catalog/catalog'
# Every schema is published side by side; shipped apps read only their own.
VERSIONS=(1,2)
EXTENSIONS={'gzip':'gz','xz':'xz'}
TABLES={1:{'metadata','stations','variants','service_days','calls'},2:{'metadata','stations','variants','variant_days','calls'}}
MIN_STATIONS=2000
MIN_VARIANTS=5000

def base(version): return f'{ROOT}/v{version}'

def folder_version(folder):
    name=pathlib.Path(folder).name
    if not name.startswith('v') or not name[1:].isdigit() or int(name[1:]) not in VERSIONS: raise ValueError(f'Unexpected catalog folder {name}')
    return int(name[1:])

def read_url(url,limit):
    with urllib.request.urlopen(url,timeout=90) as response:
        data=response.read(limit+1)
    if len(data)>limit: raise ValueError('Oversize response')
    return data

def compression(manifest):
    # Absent means gzip, as in every schema 1 manifest.
    value=manifest.get('compression','gzip')
    if value not in EXTENSIONS or (manifest['schemaVersion']==1 and value!='gzip'): raise ValueError('Unsupported compression')
    return value

def catalog_name(manifest):
    url=urllib.parse.urlsplit(manifest['catalogURL'])
    expected=urllib.parse.urlsplit(base(manifest['schemaVersion']))
    name=f'catalog-{manifest["sha256"]}.sqlite.{EXTENSIONS[compression(manifest)]}'
    if url.scheme!='https' or url.netloc!=expected.netloc or url.path!=expected.path+'/'+name or url.query or url.fragment:
        raise ValueError('Unexpected catalog URL')
    return name

def expand(compressed,method,limit):
    if method=='gzip':
        with gzip.GzipFile(fileobj=io.BytesIO(compressed)) as z: return z.read(limit+1)
    decoder=lzma.LZMADecompressor(format=lzma.FORMAT_XZ)
    data=decoder.decompress(compressed,max_length=limit+1)
    # Apps reject truncated streams and bytes after the stream; so does publication.
    if len(data)<=limit and (not decoder.eof or decoder.unused_data): raise ValueError('Incomplete or trailing xz stream')
    return data

def verify(folder,fresh=False):
    version=folder_version(folder)
    manifest=json.loads((folder/'manifest.json').read_text())
    if manifest['schemaVersion']!=version: raise ValueError('Unsupported schema')
    if fresh:
        checked=dt.datetime.fromisoformat(manifest['sourceCheckedAt'].replace('Z','+00:00'))
        age=dt.datetime.now(dt.timezone.utc)-checked
        if age>dt.timedelta(hours=72) or age<dt.timedelta(minutes=-5): raise ValueError('Bootstrap source check is not current')
    compressed=(folder/catalog_name(manifest)).read_bytes()
    if len(compressed)!=manifest['compressedBytes'] or len(compressed)>128*1024*1024 or hashlib.sha256(compressed).hexdigest()!=manifest['sha256']: raise ValueError('Compressed catalog integrity mismatch')
    data=expand(compressed,compression(manifest),512*1024*1024)
    if len(data)!=manifest['uncompressedBytes'] or len(data)>512*1024*1024: raise ValueError('Expanded catalog size mismatch')
    with tempfile.TemporaryDirectory() as temp:
        path=pathlib.Path(temp)/'catalog.sqlite';path.write_bytes(data)
        db=sqlite3.connect(f'file:{path}?mode=ro',uri=True)
        if db.execute('PRAGMA user_version').fetchone()[0]!=version or db.execute('PRAGMA integrity_check').fetchone()[0]!='ok' or db.execute('PRAGMA foreign_key_check').fetchall(): raise ValueError('Invalid SQLite catalog')
        if {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}!=TABLES[version]: raise ValueError('Unexpected catalog tables')
        metadata=dict(db.execute('SELECT key,value FROM metadata'))
        if metadata['schemaVersion']!=str(version): raise ValueError('Metadata mismatch: schemaVersion')
        for key in ('validFrom','validThrough','sourceCheckedAt','generatedAt'):
            if metadata[key]!=manifest[key]: raise ValueError(f'Metadata mismatch: {key}')
        if version==1: span=db.execute('SELECT min(day),max(day) FROM service_days').fetchone()
        else:
            # Each calendar spans its own first through last operating day.
            malformed=db.execute("""SELECT variant_id FROM variant_days WHERE date(first_day) IS NOT first_day OR date(last_day) IS NOT last_day
              OR length(days)!=julianday(last_day)-julianday(first_day)+1 OR days NOT GLOB '1*' OR days NOT GLOB '*1' OR days GLOB '*[^01]*' LIMIT 1""").fetchone()
            if malformed: raise ValueError(f'Malformed operating calendar: {malformed[0]}')
            if db.execute('SELECT count(*) FROM variants WHERE id NOT IN (SELECT variant_id FROM variant_days)').fetchone()[0]: raise ValueError('Variant without operating calendar')
            span=db.execute('SELECT min(first_day),max(last_day) FROM variant_days').fetchone()
        if span!=(manifest['validFrom'],manifest['validThrough']): raise ValueError('Service dates mismatch manifest validity')
        broken=db.execute('''WITH times AS (
          SELECT variant_id,sequence*2 AS ordinal,arrival_day_offset*86400+arrival_seconds AS seconds FROM calls WHERE arrival_seconds IS NOT NULL
          UNION ALL SELECT variant_id,sequence*2+1,departure_day_offset*86400+departure_seconds FROM calls WHERE departure_seconds IS NOT NULL
        ), ordered AS (SELECT variant_id,seconds,lag(seconds) OVER(PARTITION BY variant_id ORDER BY ordinal) AS prior FROM times)
        SELECT variant_id FROM ordered WHERE seconds<prior LIMIT 1''').fetchone()
        if broken: raise ValueError(f'Nonchronological exported journey: {broken[0]}')
        if db.execute('SELECT count(*) FROM stations').fetchone()[0]<MIN_STATIONS: raise ValueError('Station coverage sanity gate')
        if db.execute('SELECT count(*) FROM variants').fetchone()[0]<MIN_VARIANTS: raise ValueError('Train coverage sanity gate')
        db.close()
    return manifest

def previous(folder):
    try: data=read_url(base(folder_version(folder))+'/manifest.json',64*1024)
    except urllib.error.HTTPError as e:
        # A newly added schema has no publication yet.
        if e.code==404: return
        raise
    manifest=json.loads(data);name=catalog_name(manifest)
    packed=read_url(manifest['catalogURL'],128*1024*1024)
    folder.mkdir(parents=True,exist_ok=True);(folder/name).write_bytes(packed);(folder/'manifest.json').write_bytes(data)
    verify(folder)

def prune(folder):
    keep={catalog_name(json.loads((folder/'manifest.json').read_text()))}
    if (folder/'previous-manifest.json').exists(): keep.add(catalog_name(json.loads((folder/'previous-manifest.json').read_text())))
    for path in folder.glob('catalog-*.sqlite.*'):
        if path.name not in keep: path.unlink()
    # App bundling uses this locally; Pages only distributes the compressed form.
    (folder/'NationalTimetable.sqlite').unlink(missing_ok=True)

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('command',choices=['previous','verify','prune','bootstrap']);p.add_argument('--root',type=pathlib.Path,default=pathlib.Path('site/catalog'))
    p.add_argument('--version',type=int,action='append',choices=VERSIONS,help='Repeatable; default every published schema')
    a=p.parse_args()
    for version in sorted(set(a.version or VERSIONS)):
        folder=a.root/f'v{version}'
        if a.command=='previous': previous(folder)
        elif a.command=='verify': print(json.dumps(verify(folder),indent=2))
        elif a.command=='prune': prune(folder)
        else:
            # Only schema 1 had an initial snapshot; later schemas keep their previous publication.
            source=pathlib.Path(f'bootstrap/catalog/v{version}')
            if not source.exists(): continue
            verify(source,fresh=True)
            folder.mkdir(parents=True,exist_ok=True)
            if (folder/'manifest.json').exists(): shutil.copy2(folder/'manifest.json',folder/'previous-manifest.json')
            for path in source.iterdir(): shutil.copy2(path,folder/path.name)
