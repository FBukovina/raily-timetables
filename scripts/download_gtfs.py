#!/usr/bin/env python3
"""Fetch a country's railway GTFS feeds with conditional requests and record provenance.

gtfs.de publishes DELFI e.V.'s nationwide data weekly as free CC BY 4.0 railway
feeds covering the next 30 days. Unchanged feeds are revalidated, not
re-downloaded. Every feed must open as a GTFS archive before the inventory
names it.
"""
from __future__ import annotations
import argparse, datetime as dt, email.utils, hashlib, json, pathlib, urllib.error, urllib.request, zipfile

FEEDS = {'de': {'source': 'https://gtfs.de/en/feeds/', 'license': 'CC-BY-4.0',
                'attribution': 'DELFI e.V., gtfs.de',
                'feeds': {'fv.zip': 'https://download.gtfs.de/germany/fv_free/latest.zip',
                          'rv.zip': 'https://download.gtfs.de/germany/rv_free/latest.zip'}}}
MAX_BYTES = 256 * 1024 * 1024
REQUIRED = {'agency.txt', 'routes.txt', 'trips.txt', 'stop_times.txt', 'stops.txt'}

def now():
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec='seconds').replace('+00:00', 'Z')

def check_archive(path):
    with zipfile.ZipFile(path) as archive:
        names = set(archive.namelist())
        if not REQUIRED <= names or not names & {'calendar.txt', 'calendar_dates.txt'}:
            raise ValueError(f'{path.name} is not a complete GTFS feed')
        if archive.testzip() is not None: raise ValueError(f'{path.name} has a corrupt member')

def fetch(url, target, previous):
    request = urllib.request.Request(url, headers={'User-Agent': 'raily-timetables (+https://github.com/FBukovina/raily-timetables)'})
    if previous and target.exists():
        if previous.get('etag'): request.add_header('If-None-Match', previous['etag'])
        if previous.get('lastModified'): request.add_header('If-Modified-Since', previous['lastModified'])
    try:
        with urllib.request.urlopen(request, timeout=300) as response:
            partial = target.with_suffix('.partial'); digest = hashlib.sha256(); size = 0
            with open(partial, 'wb') as out:
                while chunk := response.read(1 << 20):
                    size += len(chunk)
                    if size > MAX_BYTES: raise ValueError(f'{url} exceeds {MAX_BYTES} bytes')
                    digest.update(chunk); out.write(chunk)
            check_archive(partial); partial.replace(target)
            return {'url': url, 'etag': response.headers.get('ETag'), 'lastModified': response.headers.get('Last-Modified'),
                    'bytes': size, 'sha256': digest.hexdigest(), 'checkedAt': now()}
    except urllib.error.HTTPError as error:
        if error.code != 304: raise
        check_archive(target)
        return {**previous, 'checkedAt': now()}

def download(country, cache):
    spec = FEEDS[country]; cache.mkdir(parents=True, exist_ok=True)
    inventory_path = cache / 'inventory.json'
    previous = json.loads(inventory_path.read_text()) if inventory_path.exists() else {'feeds': {}}
    feeds = {name: fetch(url, cache / name, previous['feeds'].get(name)) for name, url in spec['feeds'].items()}
    for name, item in feeds.items():
        modified = item.get('lastModified')
        if modified and dt.datetime.now(dt.timezone.utc) - email.utils.parsedate_to_datetime(modified) > dt.timedelta(days=14):
            raise ValueError(f'{name} was last published {modified}; the free feed covers 30 days from publication')
    inventory = {'country': country.upper(), 'source': spec['source'], 'license': spec['license'],
                 'attribution': spec['attribution'], 'complete': True, 'feeds': feeds,
                 'sourceCheckedAt': min(item['checkedAt'] for item in feeds.values())}
    inventory_path.write_text(json.dumps(inventory, indent=2) + '\n')
    print(json.dumps(inventory, indent=2), flush=True)
    return inventory

if __name__ == '__main__':
    p = argparse.ArgumentParser(); p.add_argument('--country', default='de', choices=sorted(FEEDS))
    p.add_argument('--cache', type=pathlib.Path)
    a = p.parse_args(); download(a.country, a.cache or pathlib.Path('.cache/gtfs') / a.country)
