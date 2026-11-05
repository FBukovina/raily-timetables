#!/usr/bin/env python3
"""Check Raily Live from outside: HTTPS, /v1/health and a recent feed fetch.

Prints nothing and exits 0 when healthy; otherwise prints one line saying
what is wrong and exits 1. Three attempts 20 seconds apart, so one slow
response or a restart doesn't count as an outage.

    python3 scripts/relay_health.py https://150-230-152-127.sslip.io
"""
import json, sys, time, urllib.request

MAX_FEED_AGE = 10 * 60


def problem(base):
    try:
        with urllib.request.urlopen(base.rstrip('/') + '/v1/health', timeout=20) as response:
            body = json.load(response)
    except Exception as error:
        return f'{type(error).__name__}: {error}'.replace('\n', ' ')
    age = time.time() - (body.get('fetchedAt') or 0)
    if body.get('ok') and age < MAX_FEED_AGE:
        return None
    return f"ok={body.get('ok')}, last feed fetch {int(age)} s ago, last error {body.get('lastError')}"


def main(base, attempts=3, pause=20):
    for attempt in range(attempts):
        found = problem(base)
        if found is None:
            return 0
        if attempt + 1 < attempts:
            time.sleep(pause)
    print(found)
    return 1


if __name__ == '__main__':
    sys.exit(main(sys.argv[1]))
