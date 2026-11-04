#!/usr/bin/env python3
"""Natural Earth 1:10m country borders -> compact European polygons for station countries.

GTFS stops carry no country. Each catalog is authoritative for stations in its
own country, so stops are placed with these public-domain borders instead of
names. Usage: borders.py ne_10m_admin_0_countries.geojson scripts/europe-borders.json
"""
import json, sys

# Countries a European railway feed can reach, including Turkey's Thrace and Russia's Kaliningrad.
COUNTRIES = {'AD','AL','AT','BA','BE','BG','BY','CH','CZ','DE','DK','EE','ES','FI','FR','GB','GR','HR','HU','IE','IT','LI','LT','LU','LV','MC','MD','ME','MK','NL','NO','PL','PT','RO','RS','RU','SE','SI','SK','SM','TR','UA','VA','XK'}
# About 20 m; borders of the 1:10m source are not more precise than this.
TOLERANCE = 0.0002
WINDOW = (-25.0, 34.0, 45.0, 72.0)

def simplify(points, tolerance):
    """Iterative Douglas-Peucker; keeps both ends of the ring."""
    if len(points) < 4: return points
    keep = [False]*len(points); keep[0] = keep[-1] = True; stack = [(0, len(points)-1)]
    while stack:
        a, b = stack.pop(); (x1, y1), (x2, y2) = points[a], points[b]; dx, dy = x2-x1, y2-y1
        norm = (dx*dx+dy*dy) ** 0.5; best, index = -1.0, None
        for i in range(a+1, b):
            x, y = points[i]
            d = abs(dy*x-dx*y+x2*y1-y2*x1)/norm if norm else ((x-x1)**2+(y-y1)**2) ** 0.5
            if d > best: best, index = d, i
        if index is not None and best > tolerance:
            keep[index] = True; stack += [(a, index), (index, b)]
    return [p for p, k in zip(points, keep) if k]

def inside_window(ring):
    xs = [p[0] for p in ring]; ys = [p[1] for p in ring]
    return max(xs) >= WINDOW[0] and min(xs) <= WINDOW[2] and max(ys) >= WINDOW[1] and min(ys) <= WINDOW[3]

def main(source, output):
    features = json.load(open(source, encoding='utf-8'))['features']; result = {}
    for feature in features:
        code = feature['properties'].get('ISO_A2_EH')
        if code not in COUNTRIES: continue
        geometry = feature['geometry']
        polygons = geometry['coordinates'] if geometry['type'] == 'MultiPolygon' else [geometry['coordinates']]
        kept = []
        for polygon in polygons:
            if not inside_window(polygon[0]): continue
            rings = [[[round(x, 5), round(y, 5)] for x, y in simplify(ring, TOLERANCE)] for ring in polygon]
            if len(rings[0]) >= 4: kept.append(rings)
        if kept: result[code] = kept
    if set(result) != COUNTRIES: raise ValueError(f'Missing countries {sorted(COUNTRIES-set(result))}')
    payload = {'source': 'Natural Earth 1:10m Admin 0 Countries (public domain), ISO_A2_EH',
               'tolerance': TOLERANCE, 'countries': result}
    with open(output, 'w', encoding='utf-8') as f: json.dump(payload, f, separators=(',', ':'))

if __name__ == '__main__': main(*sys.argv[1:3])
