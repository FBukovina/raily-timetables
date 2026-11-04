# Raily Live relay

`scripts/live_relay.py` joins Germany's free GTFS Realtime stream to catalog runs and answers `GET /v1/runs?ids=…` for the app. See the module docstring for the API and semantics.

## Requirements

- An always-on Linux host with 2–3 GB of RAM: loading the static railway feeds peaks at about 2 GB for some seconds every six hours, and the stream decodes in about 7 s per fetch.
- About 18 GB of inbound traffic a day: the stream is 12.8 MB gzip, fetched once a minute. Outbound traffic is small JSON.
- HTTPS on a host name the app can reach. The app accepts plain HTTP only for `127.0.0.1` and `localhost`.

## Oracle Cloud Always Free

1. Create an Always Free account at cloud.oracle.com. Pick a home region near the users and the feed, such as Germany Central (Frankfurt); it can't be changed later.
2. Create an instance: image Canonical Ubuntu 24.04, shape VM.Standard.A1.Flex (Ampere) with 2 OCPUs and 12 GB, a public subnet with a public IPv4 address, and your SSH public key. The 1 GB AMD micro shape is too small for the 1.6 GB peak. "Out of host capacity" for A1 is common; retry later or choose another availability domain.
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

## Privacy

Requests carry the run IDs a person is looking at. The relay keeps no request log and the Caddy example discards access logs. It stores only the public feeds it downloads.

## Licences

The stream is CC BY-SA 4.0 (DELFI e.V., gtfs.de); every response names it in `source`, and the app shows the attribution on journeys that use it.
