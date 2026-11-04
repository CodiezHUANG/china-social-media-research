# Bilibili content design

Bilibili content collection covers video metadata, comments and XML danmaku
snapshots through separate bounded surfaces and shared records.

## Independent collection surfaces

All routes use the standard library, the shared plan/record/manifest
core, and `BILI_PSEUDONYM_SALT`. The approved BV list is validated, de-duplicated,
capped, and frozen into the execution command. No external crawler is needed.

| Surface | Requests | Output |
|---|---|---|
| detail | Public video view interface | Video metadata only |
| comments | View interface, then public reply pages | first_page: main comments plus returned nested replies; deep: main comments only |
| danmaku | View interface, then selected CIDs' public XML snapshots | Bounded danmaku records |

All Bilibili surfaces use collection. These routes never automatically
fall back on failure.

## Limits and interpretation

- At most 200 requested videos; default 20.
- Comments use `--comments-mode first_page` (default) or `deep`.
  - `first_page`: first page up to 20 main comments plus the nested replies
    returned with them; no second page is requested.
  - `deep`: first-level comments only, up to 100 and at most five pages.
- Nested replies are exported only in `first_page` mode, with `level=2` and
  `parent_id`; `deep` does not traverse nested replies.
- `--on-item-error stop` is the default. `continue` records the failed BV,
  preserves its partial records, continues to the next BV, sets status
  `complete_with_failures` and exits 1.
- Danmaku: default 100 per video, hard cap 1000 across all selected parts.
- Default first part only; explicit `--max-parts` allows up to five parts.
- A per-item cap may stop sampling before all selected parts are fetched.
- A public XML snapshot is not the historical or complete danmaku population.

Public content requests are serial and separated by 5–8 seconds. Before a run,
the `view` endpoint is probed three times with two-second spacing; at least two
successes are required. Common --session auto/required/off controls optional
Bilibili authentication. Every request has a 15-second timeout and the
outer runner has a step timeout. XML responses are bounded to 2 MiB on the wire
and after decompression. Identity, gzip, wrapped DEFLATE and raw HTTP DEFLATE
are supported. DTD/entity declarations are rejected. The fixed XML endpoint is
`https://api.bilibili.com/x/v1/dm/list.so?oid=<cid>`; certificate verification
remains enabled. No automatic credential acquisition, signing, proxy
inheritance, retry, alternate endpoint, or access-control workaround is
implemented.

## Data and failure handling

Every independent run creates `records.jsonl`, its
`bilibili_<surface>_manifest.json`, and the outer `run_manifest.json`.
records use schema `0.2`, UTC publication timestamps, capture timestamps,
public object IDs, whitelisted platform attributes, and salted author pseudonyms.
Raw author names, user IDs, danmaku author hashes, and salts are not exported.
Free text still needs appropriate handling.

Video identity, comment root/parent, page numbers, IDs, timestamps, CID matching,
and XML structure are checked before export. A valid zero-comment/zero-danmaku
response differs from a missing payload or HTML challenge. First-level comment
IDs and danmaku IDs are de-duplicated within each video's sample.

The manifest records bounds, `comments_mode`, `nested_reply_count`, completed
object/page counts, HTTP request count, the connectivity precheck, and why each
bounded sample stopped. Failures carry `failure_class` (`rate_limited`,
`env_blocked`, `platform_invisible`, `pagination_unavailable`, `schema_changed`, or `access_restricted`
for login/session errors) alongside the existing `error_kind`, `api_code`,
`http_status`, and transport type. Default per-item policy stops the run;
explicit continue preserves the failed video's partial output and proceeds
to the next selected BV, while keeping a nonzero final result. Failure logs identify the object and partial progress
without writing server messages, response bodies, or credentials.

## Research audit

`cmc bili-audit --run <run-dir> [--run <run-dir> ...] --output <new-dir>`
builds per-video canonical layers from existing runs:

- `videos/<bvid>/video_info.json`, `comments.json`, `danmaku.json`;
- `quality_audit.csv` with layer presence, completion, retry needs, counts and failure class/stage;
- `retry_queue.jsonl` for `rate_limited`, `env_blocked` and `pagination_unavailable` failures;
- `audit.json` with source runs, BVID/layer counts and a provenance note.

It never overwrites source runs and never writes credential values. Pseudonyms
may use different per-run salts, so author-level comparisons must not cross
runs. This audit is local research material; it is not a new online sample and
does not establish complete comment or danmaku coverage.

`cmc bili-supplement --audit <audit-dir> --output <new-dir> [--layer ...]
[--retry-only]` previews missing-layer and retry targets; add `--execute` to
collect them. Partial layers are included even when some records are already
present. It uses `--on-item-error continue` and writes a
`supplement_manifest.json` with per-layer targets/results.
