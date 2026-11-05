# Raily Live relay

`scripts/live_relay.py` joins Germany's free GTFS Realtime stream to catalog runs and answers `GET /v1/runs?ids=…` for the app. It also answers the app's Prague (PID) requests under `/v1/pid/` from `scripts/pid_relay.py`: it asks Golemio once with Raily's key, shares each answer between phones for 10 to 20 seconds (timetables for six hours), and sends only trains from the position list, about 11 kB gzip instead of 230 kB. See the module docstrings for the API and semantics.

The app asks the relay first without the key, and asks Golemio itself with the key built into the app when the relay is unreachable, failing, or answers without `X-Raily-Source: golemio`. The relay omits that header when it has no key or Golemio refuses it, so a relay without a key costs a phone one quick request every two minutes.

## Requirements

- An always-on Linux host with 2–3 GB of RAM: loading the static railway feeds peaks at about 2 GB for some seconds every six hours, and the stream decodes in about 7 s per fetch.
- About 18 GB of inbound traffic a day: the stream is 12.8 MB gzip, fetched once a minute. Prague adds at most a 230 kB position list every 15 seconds while someone looks at it, plus the boards and trips people open. Outbound traffic is small JSON.
- Raily's Golemio key for Prague, in a file: `RAILY_GOLEMIO_KEY_FILE=<file> deploy/live-relay/deploy.sh …` installs it as `/etc/raily/golemio-key`, readable only by root and the relay. It is the key in the app's ignored `Config/Secrets.xcconfig` (`GOLEMIO_API_KEY`); never commit it.
- HTTPS on a host name the app can reach. The app accepts plain HTTP only for `127.0.0.1` and `localhost`.

## Oracle Cloud Always Free

1. Create an Always Free account at cloud.oracle.com. Pick a home region near the users and the feed, such as Germany Central (Frankfurt); it can't be changed later.
2. Create an instance: image Canonical Ubuntu 24.04, shape VM.Standard.A1.Flex (Ampere) with 1 OCPU and 6 GB (memory use then stays above the 20% Oracle treats as idle), a public subnet with a public IPv4 address, and your SSH public key. The 1 GB AMD micro shape is too small for the 1.6 GB peak. "Out of host capacity" for A1 is common; retry later or choose another availability domain.
3. In the instance's subnet, add ingress rules to the default security list for TCP 80 and 443 from 0.0.0.0/0.
4. Deploy, then set the app's `RAILY_LIVE_URL` for Release to the printed address:

```sh
deploy/live-relay/deploy.sh ubuntu@<public-ip>
```

Always Free A1 capacity is 4 OCPUs and 24 GB per account. Oracle reclaims Always Free instances it judges idle, by low CPU, network and memory use over about a week, and the relay is light enough to look idle (about 2% of one core). Upgrading the account to Pay As You Go keeps Always Free resources free and exempts them from idle reclamation; check Oracle's current terms. Otherwise expect to recreate the VM and re-run the deploy.

## Run elsewhere

```sh
docker build -f deploy/live-relay/Dockerfile -t raily-live .
docker run -d --restart=always -p 127.0.0.1:8080:8080 -v raily-live-cache:/srv/raily/cache raily-live
```

or install `raily-live.service` and put `Caddyfile` in front. `GET /v1/health` returns 503 until the first fetch succeeds and whenever the last successful fetch is over five minutes old; point an uptime monitor at it.

## Monitoring

`.github/workflows/relay-health.yml` checks `/v1/health` from GitHub every 10 minutes with `scripts/relay_health.py`: HTTPS must work, `ok` must be true and the last feed fetch must be under 10 minutes old, with three attempts 20 seconds apart. An outage opens one `relay-down` issue and fails the run, so GitHub notifies the owner; recovery closes the issue. If it fires: `ssh ubuntu@<ip> 'systemctl status raily-live caddy; journalctl -u raily-live -n 50'`, and re-run `deploy.sh` to reinstall.

## Privacy

Requests carry the run IDs, trips and stations a person is looking at. The relay keeps no request log and the Caddy example discards access logs. It stores only the public feeds it downloads and, in memory for at most six hours, the Golemio answers it shares. Phones never send the Golemio key to the relay.

## Licences

The stream is CC BY-SA 4.0 (DELFI e.V., gtfs.de); every response names it in `source`, and the app shows the attribution on journeys that use it. Prague answers are Golemio's PID open data, passed on unchanged apart from leaving out vehicles that aren't trains.
