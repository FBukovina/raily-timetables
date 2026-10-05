# Raily national timetables

A credential-free, schedule-only railway catalog for Czechia. The importer reads **the complete annual archive plus every listed amendment and cancellation** from the Ministry of Transport's CIS JŘ publication of Správa železnic data. It does not contain Raily application source, credentials, live train positions, bus services, or a transfer planner.

Production manifest: [catalog/v1/manifest.json](https://fbukovina.github.io/raily-timetables/catalog/v1/manifest.json).

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

GitHub Actions runs daily around **05:17 Europe/Prague** and on manual dispatch. A full valid output is uploaded as one Pages artifact, so failures leave the previous deployment intact. Each artifact retains the current and preceding immutable gzip catalog, and the preceding manifest. `source-checks/latest.json` records the complete checked input inventories and output counts. A checked-in `bootstrap/` snapshot is used only for initial deployment, is independently verified, and expires for bootstrap use after 72 hours; daily updates always download and validate authoritative source files.

For an app bundle, use the generated `site/catalog/v1/NationalTimetable.sqlite` and copy `manifest.json` to `NationalTimetableManifest.json` before running `publish.py prune`. SQLite queries require no server or token.

## Correctness and limits

* PA identity is the complete authoritative ObjectType/Company/Core/Variant/TimetableYear tuple, never just a train number. The newest `CZPTTCreation` replaces older snapshots of that same PA. Later cancellation calendars subtract days. Planned replacements reference the original PA and **original** calendar, which can differ from the replacement's dates across midnight.
* The service date belongs to the first Czech point. Each source `Offset` is retained as an explicit number of local midnight crossings, including negative offsets. `Time` is the timetable civil clock: its fixed `+01:00` serialization suffix is not converted into summer UTC time. Consumers must resolve civil dates in Europe/Prague and handle DST ambiguity without inventing an instant.
* Explicit `CZInconsistentTime` paths with decreasing arrival/departure clocks are processed but omitted from passenger results because the app cannot truthfully model their operational countertime semantics; their count is recorded in the source-check report. Unmarked decreasing clocks fail publication.
* Boarding-only and alighting-only restrictions override general passenger-stop markers. Technical points are not passenger calls. Repeated stations remain distinct ordered calls. Nonpublic and replacement-bus links split public railway segments rather than inventing a through train connection.
* Whole-run cancellation is supported. Supplied end-section cancellation is supported, retaining boundary calls and the original service-day offsets. Ambiguous repeated cancellation endpoints and middle-section cancellations fail closed. The current official specification states partial cancellations are generally **not published to CIS JŘ**, so the catalog cannot reconstruct unavailable operational changes.
* Facilities retain exact occurrence-based section applicability and their own calendars; unresolved applicability is omitted. Coordinates are absent rather than inferred. Operator names come only from official KADR RICS codebook entries; ambiguous RICS company names are shown as the source code.
* This is a published timetable snapshot, not a guarantee of service or live operating status. Out-of-validity queries must not produce new departures. Catalog refresh failure should preserve installed data and saved journey snapshots.

## Schema v1

`PRAGMA user_version=1`. `metadata(key,value)` contains `schemaVersion`, `validFrom`, `validThrough`, `generatedAt`, `sourceCheckedAt`, and `sourceURL`. `stations` contains `id` (`CZ:57076` style), `name`, accent/punctuation-folded `normalized_name`, `country`, `code`, and nullable `latitude`/`longitude`. `variants` contains authoritative `id`, `train_number`, `title`, `operator_name`, `destination`, and `facilities_json`. Derived contiguous segments append `:section:start-end` to PA identity. `service_days(variant_id,day)` gives ISO operating dates. `calls` contains `variant_id`, ordered `sequence`, `station_id`, nullable arrival/departure `_seconds` and `_day_offset`, and `allows_boarding`/`allows_alighting` flags. Number, day, station, and normalized station-name indexes support local search.

Facility JSON is a list of `{code,startSequence,endSequence,startStationName,endStationName,calendar?}`. Sequences refer to exported passenger calls; `calendar`, when present, is `{start:"YYYY-MM-DD",bitmap:"0101..."}`. An absent calendar means the path's own operating dates. Codes are official central timetable note codes.

The manifest's camelcase fields are `schemaVersion`, `generatedAt`, `sourceCheckedAt`, `validFrom`, `validThrough`, `catalogURL`, `compressedBytes`, `uncompressedBytes`, and `sha256` (the **compressed** file digest). `sourceURL` is supplementary attribution. Immutable gzip filenames contain that digest.

## Sources and reuse

* [Ministry of Transport source and reuse terms](https://md.gov.cz/Dokumenty/Verejna-doprava/Jizdni-rady,-kalendare-pro-jizdni-rady,-metodi-(1)/Jizdni-rady-verejne-dopravy): CIS JŘ data is available for further processing, including commercial use.
* [Official CIS railway XML directory](https://portal.cisjr.cz/pub/draha/celostatni/szdc/).
* [Message specification v1.09.06](https://portal.cisjr.cz/pub/draha/celostatni/szdc/Popis%20DJ%C5%98_CIS_v1_09_06.pdf), particularly §§3.1.1, 3.1.4, 3.2, 5.1, 5.3, 5.5 and 5.8.
* [Official KADR codebook service](https://provoz.spravazeleznic.cz/kadrws/ciselniky.asmx). `scripts/codebooks.json` is a minimized capture of company codes/names and commercial train categories, captured 2026-10-05. Personal contact fields are not retained.

Tests use hand-authored fictional XML based on the published schema; no test fixture is included in a production catalog. Upstream timetable data remains subject to the source's reuse terms. This repository does not relicense upstream data.
