# Bilibili search

The Bilibili search collector exports bounded ranked video results using the
Python standard library, shared records and execution controls.

## Routing and use

`cmc login --platform bilibili` opens an ordinary dedicated browser for manual
login and saves platform-scoped state outside the checkout. It shares the
project-owned lifecycle with Douban but has a separate private directory.
The browser library is needed for login only; collection uses the standard
library. The same command supports --status and --clear.

Common --session modes control search and content. Auto prefers an explicit
BILIBILI_COOKIE, then saved state, then anonymous only if no state exists.
Required stops before network access if state is missing; off ignores both
credential sources. Invalid or expired state fails without anonymous fallback.
Preflight --session required checks state offline, not online validity.

Cookies are sent only to HTTPS api.bilibili.com on its normal port, filtered
by cookie domain, path and expiry. Redirects remain rejected. Plans and logs
never contain credential values; manifests record only session_source and
cookie_used, with safe manual-login hints on authentication errors.
No automatic relogin or retry is introduced. Authenticated two-page comments,
ten-video first-page main/nested comments and a two-part XML sample have live
evidence. The failed video from the ten-video deep run subsequently completed
three pages/52 comments through supplement, and its audit marks the comment
layer complete. See [content evidence](BILIBILI_CONTENT.md).

Bilibili uses collection for all surfaces. No automatic fallback
occurs within a run.
Set `BILI_PSEUDONYM_SALT` privately before execution.

```powershell
cmc preflight --platform bilibili --surface search
cmc plan --platform bilibili --query 'example film' --max-items 20 --max-pages 2 --output '.\runs\bili-search'
```

Preview does not collect. Execute a reviewed plan with `cmc collect` and
`--execute`. Both host plugins and old script entry points share this routing.

## Bounds and data

The fixed public search URL is
`https://api.bilibili.com/x/web-interface/search/type`, with video search type.
This site interface is not a promised stable developer API.

Each HTTP page requests 20 entries. The requested item limit caps exported
unique videos; at most five pages and 200 exported records are allowed.
`ketang` course cards are explicitly outside the video sampling frame, with
positions and exclusion reasons logged. Unknown types and malformed videos
stop the run. Original search positions are preserved after course exclusion.

Outputs are `records.jsonl`, `bv_candidates.txt`,
`bilibili_manifest.json`, and the outer `run_manifest.json`. The records and BV
frame align in order and count. Schema 0.2 includes UTC publication time,
capture time, public engagement fields, and salted uploader pseudonyms.
Raw uploader IDs/names, API bodies, credentials, and server messages are not
written. Public free text still needs appropriate handling.

Requests are serial, with 2.5–5 second spacing, 15-second request
timeouts, response size limits, and an outer step timeout. Access challenges,
redirects, HTTP/API errors, and schema changes stop the run while preserving
partial output. A selected session may supply scoped cookies. No retries,
signing, proxy inheritance, or alternate endpoint fallback are implemented.
