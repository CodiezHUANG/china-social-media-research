# Weibo collection design

Hot-list snapshots, first-level comments, and keyword search are independently
implemented in `platforms/weibo.py`. They use the shared bounded JSON
transport, plain-text conversion, records, privacy, and runner. No third-party
crawler code is embedded. Search, hot snapshots and comments share one
platform adapter.

## Entry points

```powershell
cmc preflight --platform weibo --surface hot
cmc preflight --platform weibo --surface comments --visitor-session
cmc preflight --platform weibo --surface search --visitor-session
cmc plan --platform weibo --surface hot --max-items 20 --output '.\runs\weibo-hot'
cmc plan --platform weibo --surface comments --visitor-session --input-file '.\approved_posts.txt' --max-items 1 --comments-per-item 20 --max-pages 2 --output '.\runs\weibo-comments'
cmc plan --platform weibo --surface search --query 'keyword' --visitor-session --max-items 20 --max-pages 2 --output '.\runs\weibo-search'
```

The hot surface needs no credential, salt, browser or Cookie. Comments and
search require a private `WEIBO_PSEUDONYM_SALT` because exported posts carry an
author pseudonym. The public mobile endpoints may redirect to an anonymous
visitor page; `--visitor-session` lets the collector obtain that page's own
visitor cookies with headless Playwright before making the bounded stdlib
requests. It does not log in, use a user Cookie, solve a CAPTCHA or retry.
A saved login session can be selected with `--session auto|required|off`; use
`--session off` to ignore an expired saved session and try the visitor path
explicitly. There is no automatic fallback on failure.

## Semantics

- Hot collection is one capture-time snapshot, not a historical series. At most
  200 unique topics are exported. Original list positions and ad labels are
  preserved. Heat values are platform indicators, not audience counts.
- Comments use the public hotflow interface and `max_id` cursor, capped at
  1000 unique comments per post and fifty pages. Cursor types are carried to the
  next request, repeated cursors stop the run, and IDs are de-duplicated.
- Search uses the mobile `container/getIndex` result stream, one or more
  keywords, a per-keyword item cap and a page cap. It exports post ID, text,
  UTC-normalized timestamp, author pseudonym, engagement counts, page/rank and
  retweet marker. Cards without a post are skipped; duplicate pages end the
  keyword sample.
- Embedded nested comments are neither traversed nor exported. HTML tags are
  removed, entity text and inline image emoji alt text are preserved. Known
  platform timestamp strings are normalized to UTC; unknown formats fail.
- All outputs use schema 0.2. Raw user IDs/names, credentials, salts, and
  response bodies are not saved. Public post and comment text still needs
  appropriate handling; pseudonymization is not full anonymization.

Every run writes `records.jsonl`, `weibo_manifest.json`, and the outer
`run_manifest.json`. The manifest records sample bounds, returned/
accepted/duplicate counts, page/object progress, stop reasons, and safe errors.
Partial output survives failures. Invalid or missing data never becomes an
empty successful sample; a valid empty list with a terminal cursor is distinct.
Redirect diagnostics retain the target host and path category. HTTP errors
retain safe `Retry-After` and `RateLimit` headers when supplied.

The fixed endpoints are `https://weibo.com/ajax/side/hotSearch`,
`https://m.weibo.cn/comments/hotflow`, and
`https://m.weibo.cn/api/container/getIndex`. Requests use the selected
anonymous/visitor/saved session, are serial, spaced 2.5–5 seconds apart for
hot/comments and 8–12 seconds for search, bounded to 2 MiB, and timed out after
15 seconds. The
optional visitor session obtains the site's own anonymous cookies and closes
before API requests. Manual login separately uses a browser to save user state.
HTTP errors, redirects,
API/login responses, schema changes, and connection failures stop collection.
There are no retries, proxies, account changes, or challenge solving.

## Anonymous visitor session

Weibo's comment and search endpoints may redirect to
`visitor.passport.weibo.cn` before returning JSON. With explicit
`--visitor-session`, a headless Playwright Chromium
opens `https://m.weibo.cn/`, waits at most 20 seconds for the site to set its
own `SUB`/`SUBP` visitor cookies, then closes. Only cookies for `.weibo.cn` are
put into an in-memory header for `m.weibo.cn`; no user Cookie, saved login,
disk state, CAPTCHA handling or retry is involved. If the visitor page does not
produce cookies, the run fails with `visitor_session_unavailable` and writes no
comment records. The manifest records `visitor_session=true`,
`session_source=visitor` and `cookie_used=false`; cookie values are never
serialized.

## Manual login and saved session

`cmc login --platform weibo` opens a dedicated visible Chromium window at
`https://m.weibo.cn/`. The user logs in manually; the session is saved only
after the site sets a real login marker (`MLOGIN=1` together with `SUB`), not
merely the anonymous visitor `SUB`. The state is private, platform-scoped and
outside the repository.

Comments and search accept `--session auto` (default), `required`, or `off`:

- `auto`: an explicit `WEIBO_COOKIE` takes precedence, then a saved session,
  then anonymous visitor/anonymous collection.
- `required`: fail before requests when no valid saved/cookie session exists.
- `off`: ignore saved state and explicit Cookie.

A saved login session is used for the comment requests through the same
`m.weibo.cn` host/path/expiry-scoped cookie header as the visitor path.
Manifests record `session_source` (`saved`/`cookie`/`visitor`/`anonymous`) and
`cookie_used`; values are never serialized. The saved session can unlock later
comment pages that the anonymous visitor session cannot.

## Default routing

- `weibo/hot`, `weibo/comments`, and `weibo/search` use the Weibo collector.
- search defaults to a five-page budget and 20 exports per keyword;
  explicit limits allow up to fifty pages and 200 exports per keyword. Each
  keyword is a separate object in the manifest.
- search has saved-session and visitor live samples, including later
  film/technology queries. A 432 or login/session error stops the run.
