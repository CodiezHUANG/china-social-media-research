# Maoyan collection design

The Maoyan collector exports bounded short reviews from selected tag views.
It uses the shared HTTP transport, privacy controls, records and runner.

## Usage

Set `MAOYAN_PSEUDONYM_SALT` privately before executing a collection.

```powershell
cmc preflight --platform maoyan
cmc plan --platform maoyan --item-id '<MOVIE_ID>' --tags '0,1' --max-items 20 --max-pages 2 --output '.\runs\maoyan'
```

Preview remains offline and live execution requires
`cmc collect` with `--execute`. There is no automatic fallback.

## Sampling contract

The fixed public endpoint is
`https://m.maoyan.com/mmdb/comments/movie/<MOVIE_ID>.json` with explicit `tag`
and record `offset`. It is a site interface, not a stable developer API promise.

One run targets one verified movie, at most ten unique selected tag views,
and at most five pages per tag. Offsets are 0, 15, 30, 45, 60. The
`--max-items` caps unique exported reviews across the whole run, default 20,
hard limit 200. It is not a per-tag cap. Empty pages, reported view ends,
page limits, and the global item cap have distinct logged stop reasons.

Tag values are site view identifiers. The endpoint returns no view labels and
the collector assigns none; every Maoyan plan repeats this in `notes`. Confirm
what each selected view shows on the movie page before interpreting results.

Views may overlap and reported totals must not be added. Reviews are
de-duplicated across pages and views by movie/review ID. If an ID is absent,
a labelled fingerprint uses text, UTC publication time, city, score, and the
salted author pseudonym. Fingerprints are not guaranteed unique review IDs.

Author IDs are preferred for pseudonyms; nickname-only fallback is explicitly
labelled and cannot establish unique-person identity. No raw author ID,
nickname, salt, profile URL, or API response is saved. Free review text and
city fields still need appropriate data handling.

Known complete platform timestamps (`startTime`, otherwise `time`) are
interpreted as UTC+08:00 and normalized to UTC. Missing/partial timestamps are
not guessed. Ratings retain the platform's 0–5 scale. Only a field whitelist
is exported to schema-0.2 `records.jsonl`.

## Reliability and outputs

Requests are anonymous, serial, spaced 2.5–5 seconds apart, bounded to 2 MiB,
and have 15-second timeouts plus the outer step timeout. HTTP/API failures,
redirects, malformed data, mismatched movie IDs, invalid timestamps/ratings,
and connection failures stop the run. No retries, proxies, Cookie handling,
account rotation, or access-control bypass are implemented.

Outputs are `records.jsonl`, `maoyan_manifest.json`, and the outer
`run_manifest.json`. Manifests record limits, tag/page accounting, duplicates,
capture times, reported totals, partial progress, and safe errors. They never
persist the pseudonym salt; no checkpoint with credential material is created.
Existing runs are never overwritten or appended to.
