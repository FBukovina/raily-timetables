#!/usr/bin/env python3
"""Validate Pages artifacts and preserve the preceding immutable catalog."""
import argparse, datetime as dt, gzip, hashlib, json, pathlib, shutil, sqlite3, tempfile
import urllib.error, urllib.parse, urllib.request
BASE='https://raw.githubusercontent.com/FBukovina/raily-timetables/catalog/catalog/v1'

def read_url(url,limit):
    with urllib.request.urlopen(url,timeout=90) as response:
        data=response.read(limit+1)
    if len(data)>limit: raise ValueError('Oversize response')
    return data

def catalog_name(manifest):
    url=urllib.parse.urlsplit(manifest['catalogURL'])
    expected=urllib.parse.urlsplit(BASE)
    name=f'catalog-{manifest["sha256"]}.sqlite.gz'
    if url.scheme!='https' or url.netloc!=expected.netloc or url.path!=expected.path+'/'+name or url.query or url.fragment:
        raise ValueError('Unexpected catalog URL')
    return name

def verify(folder,fresh=False):
    manifest=json.loads((folder/'manifest.json').read_text())
    if manifest['schemaVersion']!=1: raise ValueError('Unsupported schema')
    if fresh:
        checked=dt.datetime.fromisoformat(manifest['sourceCheckedAt'].replace('Z','+00:00'))
        age=dt.datetime.now(dt.timezone.utc)-checked
        if age>dt.timedelta(hours=72) or age<dt.timedelta(minutes=-5): raise ValueError('Bootstrap source check is not current')
    compressed=(folder/catalog_name(manifest)).read_bytes()
    if len(compressed)!=manifest['compressedBytes'] or len(compressed)>128*1024*1024 or hashlib.sha256(compressed).hexdigest()!=manifest['sha256']: raise ValueError('Compressed catalog integrity mismatch')
    with gzip.GzipFile(fileobj=__import__('io').BytesIO(compressed)) as z: data=z.read(512*1024*1024+1)
    if len(data)!=manifest['uncompressedBytes'] or len(data)>512*1024*1024: raise ValueError('Expanded catalog size mismatch')
    with tempfile.TemporaryDirectory() as temp:
        path=pathlib.Path(temp)/'catalog.sqlite';path.write_bytes(data)
        db=sqlite3.connect(f'file:{path}?mode=ro',uri=True)
        if db.execute('PRAGMA user_version').fetchone()[0]!=1 or db.execute('PRAGMA integrity_check').fetchone()[0]!='ok' or db.execute('PRAGMA foreign_key_check').fetchall(): raise ValueError('Invalid SQLite catalog')
        metadata=dict(db.execute('SELECT key,value FROM metadata'))
        for key in ('validFrom','validThrough','sourceCheckedAt','generatedAt'):
            if metadata[key]!=manifest[key]: raise ValueError(f'Metadata mismatch: {key}')
        if db.execute('SELECT min(day),max(day) FROM service_days').fetchone()!=(manifest['validFrom'],manifest['validThrough']): raise ValueError('Service dates mismatch manifest validity')
        broken=db.execute('''WITH times AS (
          SELECT variant_id,sequence*2 AS ordinal,arrival_day_offset*86400+arrival_seconds AS seconds FROM calls WHERE arrival_seconds IS NOT NULL
          UNION ALL SELECT variant_id,sequence*2+1,departure_day_offset*86400+departure_seconds FROM calls WHERE departure_seconds IS NOT NULL
        ), ordered AS (SELECT variant_id,seconds,lag(seconds) OVER(PARTITION BY variant_id ORDER BY ordinal) AS prior FROM times)
        SELECT variant_id FROM ordered WHERE seconds<prior LIMIT 1''').fetchone()
        if broken: raise ValueError(f'Nonchronological exported journey: {broken[0]}')
        if db.execute('SELECT count(*) FROM stations').fetchone()[0]<2000: raise ValueError('Station coverage sanity gate')
        if db.execute('SELECT count(*) FROM variants').fetchone()[0]<5000: raise ValueError('Train coverage sanity gate')
        db.close()
    return manifest

def previous(folder):
    try: data=read_url(BASE+'/manifest.json',64*1024)
    except urllib.error.HTTPError as e:
        if e.code==404: return
        raise
    manifest=json.loads(data);name=catalog_name(manifest)
    packed=read_url(manifest['catalogURL'],128*1024*1024)
    folder.mkdir(parents=True,exist_ok=True);(folder/name).write_bytes(packed);(folder/'manifest.json').write_bytes(data)
    verify(folder)

def prune(folder):
    keep={catalog_name(json.loads((folder/'manifest.json').read_text()))}
    if (folder/'previous-manifest.json').exists(): keep.add(catalog_name(json.loads((folder/'previous-manifest.json').read_text())))
    for path in folder.glob('catalog-*.sqlite.gz'):
        if path.name not in keep: path.unlink()
    # App bundling uses this locally; Pages only distributes the compressed form.
    (folder/'NationalTimetable.sqlite').unlink(missing_ok=True)

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('command',choices=['previous','verify','prune','bootstrap']);p.add_argument('--folder',type=pathlib.Path,default=pathlib.Path('site/catalog/v1'))
    a=p.parse_args()
    if a.command=='previous': previous(a.folder)
    elif a.command=='verify': print(json.dumps(verify(a.folder),indent=2))
    elif a.command=='prune': prune(a.folder)
    else:
        source=pathlib.Path('bootstrap/catalog/v1'); verify(source,fresh=True)
        a.folder.mkdir(parents=True,exist_ok=True)
        if (a.folder/'manifest.json').exists(): shutil.copy2(a.folder/'manifest.json',a.folder/'previous-manifest.json')
        for path in source.iterdir(): shutil.copy2(path,a.folder/path.name)
