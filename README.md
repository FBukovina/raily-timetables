# Raily national timetables

A credential-free, schedule-only railway catalog for Czechia. The importer reads **the complete annual archive plus every listed amendment and cancellation** from the Ministry of Transport's CIS JŘ publication of Správa železnic data. It does not contain Raily application source, credentials, live train positions, bus services, or a transfer planner.

Production manifests: [catalog/v2/manifest.json](https://raw.githubusercontent.com/FBukovina/raily-timetables/catalog/catalog/v2/manifest.json) (schema 2, xz) and, for app builds before schema 2, [catalog/v1/manifest.json](https://raw.githubusercontent.com/FBukovina/raily-timetables/catalog/catalog/v1/manifest.json) (schema 1, gzip). One import writes both; each folder is verified and retained separately.

## Build and publish

Python 3.9+ and `curl`, with no Python package dependencies:

```sh
python3 -m unittest discover -s tests -v
python3 scripts/download.py
python3 scripts/publish.py previous
python3 scripts/catalog.py
python3 scripts/publish.py verify
```

Current and announced upcoming timetable-year archives are discovered automatically, so December rollover does not require a code change. `--year 2026` can pin a single archival import. The first import downloads over 170,000 small amendment files. Four persistent HTTPS workers save compressed payloads in monthly SQLite caches; successful chunks survive interruption. Subsequent runs conditionally revalidate old files using ETag/Last-Modified, including existing PA URLs whose content may have changed. Downloads less than six hours old are reused for resumability. Download, decompression, inventory, parse, calendar, coverage, SQLite, and SHA-256 failures stop publication. No partial catalog is advertised.

GitHub Actions runs daily around **05:17 Europe/Prague** and on manual dispatch. A full valid output is published to the dedicated generated `catalog` data branch and uploaded as one Pages artifact. The primary endpoint uses `raw.githubusercontent.com`: the account’s pre-existing Pages custom domain redirects project Pages to an unrelated site, and this importer does not modify that domain. The branch contains only generated current/previous snapshots and is updated with an explicit expected-SHA force-with-lease; source code on `main` is never rewritten. Failures leave the previous published catalog intact. Each schema folder retains its current and preceding immutable compressed catalog, and the preceding manifest. Publication verifies every schema folder, because the data branch is replaced as a whole. `source-checks/latest.json` records the complete checked input inventories and output counts. A checked-in `bootstrap/` snapshot is used only for initial deployment, is independently verified, and expires for bootstrap use after 72 hours; daily updates always download and validate authoritative source files.

For an app bundle, copy the published `site/catalog/v2/catalog-<sha256>.sqlite.xz` to `NationalTimetable.sqlite.xz` and `manifest.json` to `NationalTimetableManifest.json`. The app checks the compressed file against the manifest's digest. `publish.py prune` removes the uncompressed `NationalTimetable.sqlite` each folder keeps for inspection. SQLite queries require no server or token.

## Correctness and limits

* PA identity is the complete authoritative ObjectType/Company/Core/Variant/TimetableYear tuple, never just a train number. The newest `CZPTTCreation` replaces older snapshots of that same PA. Later cancellation calendars subtract days. Planned replacements reference the original PA and **original** calendar, which can differ from the replacement's dates across midnight.
* The service date belongs to the first Czech point. Each source `Offset` is retained as an explicit number of local midnight crossings, including negative offsets. `Time` is the timetable civil clock: its fixed `+01:00` serialization suffix is not converted into summer UTC time. Consumers must resolve civil dates in Europe/Prague and handle DST ambiguity without inventing an instant.
* Explicit `CZInconsistentTime` paths with decreasing arrival/departure clocks are processed but omitted from passenger results because the app cannot truthfully model their operational countertime semantics; their count is recorded in the source-check report. If informational foreign clocks contradict an otherwise chronological Czech schedule, that entire variant is omitted and counted in the report. Unmarked decreasing source clocks are quarantined too, with their exact PA identifiers, operating dates and reason recorded in `excludedVariants`; clocks are never repaired by guessing. Syntax, calendar, inventory, checksum and database errors still stop the full publication.
* Boarding-only and alighting-only restrictions override general passenger-stop markers. Technical points are not passenger calls. Repeated stations remain distinct ordered calls. Nonpublic and replacement-bus links split public railway segments rather than inventing a through train connection.
* Whole-run cancellation is supported. Supplied end-section cancellation is supported, retaining boundary calls and the original service-day offsets. Ambiguous repeated cancellation endpoints and middle-section cancellations fail closed. The current official specification states partial cancellations are generally **not published to CIS JŘ**, so the catalog cannot reconstruct unavailable operational changes.
* Facilities retain exact occurrence-based section applicability and their own calendars; unresolved applicability is omitted. Coordinates are absent rather than inferred. Operator names come only from official KADR RICS codebook entries; ambiguous RICS company names are shown as the source code.
* This is a published timetable snapshot, not a guarantee of service or live operating status. Out-of-validity queries must not produce new departures. Catalog refresh failure should preserve installed data and saved journey snapshots.

## Schema v2

`PRAGMA user_version=2`. `metadata(key,value)` contains `schemaVersion`, `validFrom`, `validThrough`, `generatedAt`, `sourceCheckedAt`, and `sourceURL`. `stations` contains `id` (`CZ:57076` style), `name`, accent/punctuation-folded `normalized_name`, `country`, `code`, and nullable `latitude`/`longitude`. `variants` contains authoritative `id`, `train_number`, `title`, `operator_name`, `destination`, and `facilities_json`. Derived contiguous segments append `:section:start-end` to PA identity. `variant_days(variant_id,first_day,last_day,days)` gives each variant's ISO operating dates as one calendar: `days` has one `0`/`1` character per date from `first_day` through `last_day`, both of which are operating days, so `min(first_day)` and `max(last_day)` equal the manifest validity. `calls` contains `variant_id`, ordered `sequence`, `station_id`, nullable arrival/departure `_seconds` and `_day_offset`, and `allows_boarding`/`allows_alighting` flags. Number, station, and normalized station-name indexes support local search.

A variant runs on `:day` exactly when `:day BETWEEN first_day AND last_day AND substr(days, CAST(julianday(:day)-julianday(first_day) AS INTEGER)+1, 1)='1'`. Keep the range guard: `substr` with a start below 1 counts from the end of the string. A recursive CTE over a date range replaces a join on dated rows.

Facility JSON is a list of `{code,startSequence,endSequence,calendar?}`. Sequences refer to exported passenger calls; `calendar`, when present, is `{start:"YYYY-MM-DD",bitmap:"0101..."}`. An absent calendar means the path's own operating dates. Codes are official central timetable note codes.

The manifest's camelcase fields are `schemaVersion`, `generatedAt`, `sourceCheckedAt`, `validFrom`, `validThrough`, `catalogURL`, `compressedBytes`, `uncompressedBytes`, `sha256` (the **compressed** file digest), and `compression` (`xz`). `sourceURL` is supplementary attribution. Immutable `catalog-<sha256>.sqlite.xz` filenames contain that digest. xz uses liblzma preset 6 (8 MiB dictionary) with a CRC64 check, which Apple's Compression `COMPRESSION_LZMA` decoder reads.

On the 2026-10-05 sources, schema 2 is 70.2 MB (4.75 MB xz) where schema 1 is 333.6 MB (28.4 MB gzip), with identical operating dates, calls and stations.

### Schema v1 (legacy)

Published at `catalog/v1` until no shipped app reads it, then removed from `SCHEMAS` and `publish.VERSIONS`. It differs from v2 in three ways: `user_version=1`; a `service_days(variant_id,day)` row per operating date, with a `(day,variant_id)` index, instead of `variant_days`; and facility notes that also carry `startStationName`/`endStationName`. Its manifest has no `compression` field and its catalog is gzip (`catalog-<sha256>.sqlite.gz`).

## Germany (`catalog/de/v2`)

`scripts/download_gtfs.py` fetches gtfs.de's free [long-distance](https://gtfs.de/en/feeds/de_fv/) and [regional](https://gtfs.de/en/feeds/de_rv/) railway GTFS feeds, generated from DELFI e.V.'s nationwide data under [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/), with conditional requests, and records their ETag, Last-Modified, size and SHA-256 in `.cache/gtfs/de/inventory.json`. The free feeds are published weekly and cover 30 days from publication; a feed older than 14 days stops the build. `scripts/gtfs_catalog.py` converts them:

```sh
python3 scripts/download_gtfs.py --country de
python3 scripts/gtfs_catalog.py --inventory .cache/gtfs/de/inventory.json
python3 scripts/publish.py verify --catalog de/v2
```

* Only railway routes (`route_type` 2 and 100–117) are read. Platforms join their parent station. Calls with neither boarding nor alighting are not passenger calls. The first call has no arrival or alighting, the last no departure or boarding, as in CIS.
* Trips with fewer than two passenger calls, no boarding call or times that run backwards are quarantined in `source-check.json` with their trip ID and reason, never repaired.
* GTFS times stay civil: `HH:MM:SS` past the service day's midnight, split into seconds and a day offset. The app resolves Europe/Berlin (identical to Europe/Prague) and refuses clock-change hours, as for CIS.
* A variant's ID is a hash of its operator, line, start and ordered (source station name, offsets, restrictions), so renumbered trip IDs in next week's export keep the same run ID. Trips with identical content merge their operating days.
* Calls are stored once per stop pattern (`pattern_calls`, offsets from the variant's `start_seconds`) and `calls` is a view, so the app's schema 2 SQL is unchanged. On the 2026-10-31 feeds: 111,944 railway trips → 95,218 variants, 31,153 patterns and 410,397 pattern calls (1,318,319 calls through the view), 7,590 stations; 34.5 MB SQLite, 5.4 MB xz.
* Stations get a country from `scripts/europe-borders.json` (Natural Earth 1:10m Admin 0, public domain, simplified to about 20 m by `scripts/borders.py`), because GTFS stops have none. The app lists a catalog's stations in its own country; foreign stations appear only within journeys, or where no catalog covers that country.
* Display names use the short railway form (`S+U Berlin Hauptbahnhof` → `Berlin Hbf`, `Dülmen, Bahnhof` → `Dülmen`); `normalized_name` holds space-separated folded aliases including the source name and `Hauptbahnhof` for `Hbf`, and folds `ß` to `ss` as the app does.
* The free feeds contain no train numbers (`trip_short_name`) or headsigns. Runs are titled by their line (`ICE`, `RE 1`, `S5`) and `train_number` is empty. DELFI's own GTFS on [opendata-oepnv.de](https://www.opendata-oepnv.de/) includes more fields but requires a registered account to download.

The German steps continue on error: an outage keeps the preceding German publication and never blocks Czech publication. `mirror.py` verifies every published folder; a country folder is skipped only before its first publication.

## Sources and reuse

* [Ministry of Transport source and reuse terms](https://md.gov.cz/Dokumenty/Verejna-doprava/Jizdni-rady,-kalendare-pro-jizdni-rady,-metodi-(1)/Jizdni-rady-verejne-dopravy): CIS JŘ data is available for further processing, including commercial use.
* [Official CIS railway XML directory](https://portal.cisjr.cz/pub/draha/celostatni/szdc/).
* [Message specification v1.09.06](https://portal.cisjr.cz/pub/draha/celostatni/szdc/Popis%20DJ%C5%98_CIS_v1_09_06.pdf), particularly §§3.1.1, 3.1.4, 3.2, 5.1, 5.3, 5.5 and 5.8.
* German railway timetables: DELFI e.V. via [gtfs.de](https://gtfs.de/), [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/). Attribution required: "DELFI e.V., gtfs.de".
* Country borders: [Natural Earth](https://www.naturalearthdata.com/) 1:10m Admin 0 Countries, public domain.
* [Official KADR codebook service](https://provoz.spravazeleznic.cz/kadrws/ciselniky.asmx). `scripts/codebooks.json` is a minimized capture of company codes/names and commercial train categories, captured 2026-10-05. Personal contact fields are not retained.

Tests use hand-authored fictional XML based on the published schema; no test fixture is included in a production catalog. Upstream timetable data remains subject to the source's reuse terms. This repository does not relicense upstream data.
