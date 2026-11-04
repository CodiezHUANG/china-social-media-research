# Douban short comments

The collection path uses Python standard-library HTTP and
`HTMLParser` against one public movie subject. `reviews` means short comments
from the watched (`status=P`) view, not long-form reviews. `wantsee` selects
the want-to-watch (`status=F`) view. Neither route claims a complete
historical dataset.

Manual login uses the shared Playwright browser-session window. The
collection path itself does not launch a browser.

## Execution

Collection, login and preflight use the interpreter running the CLI by
default. Set `DOUBAN_PYTHON` to select a different interpreter explicitly;
invalid settings fail rather than silently selecting another environment.

```powershell
.\.venv\Scripts\cmc.exe preflight --platform douban
.\.venv\Scripts\cmc.exe login --platform douban
.\.venv\Scripts\cmc.exe plan --platform douban --surface reviews --item-id 1292052 `
  --max-pages 1 --max-items 3 --output '.\runs\douban-pilot'
```

Plans are previews. Append `--execute` to `cmc collect` for an online run.
`DOUBAN_PYTHON` selects the interpreter for the collection module and login
window; Playwright is only needed for manual login. An optional private
`DOUBAN_COOKIE` is passed as a scoped HTTP cookie and is never serialized in
the plan.

## Manual login and automatic session reuse

`cmc login --platform douban` opens a separate, ordinary visible Chromium
context. The user performs login manually; the tool does not read passwords or
import the regular Chrome/Edge profile. Return to `movie.douban.com` after
logging in. Detection of a non-expired Douban `dbcl2` cookie triggers a scoped
storage-state save and closes the window. This presence check is not proof
that every collection route is accessible. The default wait is 300 seconds,
configurable from 15 to 1800 via `--timeout-seconds`; close the window or
interrupt to cancel.

Windows saves to `%LOCALAPPDATA%\ChinaMediaCrawler\auth\douban\storage_state.json`.
Other systems use `~/.local/state/ChinaMediaCrawler/auth/douban/storage_state.json`.
State contains credentials: Windows uses the local user's application-data
directory permissions, not application-level encryption. POSIX files are
created with mode 0600 in a mode-0700 leaf directory. Save uses atomic replace;
failed login, timeout, cancellation, or failed replacement preserve older
state. Linked locations and locations inside the checkout are rejected. Only
Douban cookies and HTTPS Douban origins are retained. State is excluded from
source, manifests, test reports, release artifacts, and printed output.

`cmc login --platform douban --status` checks state-file presence and shape
offline; it does not assert online validity. `--clear` removes only that file.

Collection uses the common `--session` choices:

- `auto` (default): an explicit `DOUBAN_COOKIE` takes precedence, otherwise use
  saved state if present, otherwise perform anonymous collection.
- `required`: require an explicit Cookie or saved state before requests.
- `off`: anonymous collection; ignore both the explicit Cookie and saved state.

Cookies are filtered by Douban host, path and expiry. Collection requires a
`dbcl2` value in the scoped header. Corrupt state fails explicitly instead of
silently changing to anonymous. Manifests record only `session_source`
(cookie/saved/anonymous) and `cookie_used`. Login/security pages stop
collection and set `login_action_required` with a safe next-command hint. No
automatic relogin, challenge solving, retries or post-failure continuation is
introduced. Existing runs are not overwritten.

## Bounds and output

- Exactly one numeric subject ID; 1–5 comment pages of at most 20 rows each.
- `--max-items` is a global unique export cap, 1–200, default 20. HTTP reads a
  page; the cap limits exported records, not server page size. For a two-page
  sample, choose a cap above 20 as well as `--max-pages 2`.
- Duplicate content/timestamp pairs are exported once. The manifest records
  duplicate counts, per-page returned/accepted counts, and the stop reason.
- Inter-page waits are 2.5–5 seconds. HTTP requests time out after 15 seconds;
  the shared runner also enforces its configured step timeout.
- `douban_<surface>.jsonl` contains canonical records. Raw author names are not
  saved. If `DOUBAN_PSEUDONYM_SALT` is set, display names become salted
  pseudonyms; otherwise the pseudonym is null. Display names are not unique IDs.
- Known timestamps (`YYYY-MM-DD HH:MM:SS`, minute or date forms) are interpreted
  as UTC+8 and normalized to UTC ISO strings; unknown formats are preserved.
  Vote text is converted to an integer when it contains digits (commas allowed);
  non-numeric values are omitted.
- `douban_manifest.json` records status, `transport=http`, counters, and
  sanitized failure metadata. `run_manifest.json` records the shared execution
  result.

## Parsing and failure behavior

The parser reads server-rendered `div.comment-item` blocks and extracts author
link, rating class, comment time, location, vote count and `span.short`.
HTTP errors, unexpected redirects, login or challenge markers, missing
comment containers, and malformed/oversized rows stop collection. There are no
retries, certificate exceptions, challenge solvers, or route fallbacks. A real
empty comments container is a valid empty sample. A later page failure
preserves records and completed-page counters already written.

Failure metadata distinguishes session loading, subject navigation, comment
navigation, extraction and output writing. Expected collection errors exit 2
and write a failed manifest. Diagnostics retain HTTP status, redirect host and
path category, safe rate-limit headers and transport error type. The exact final host
`sec.douban.com` is treated as an access/login challenge; HTTP 200 on a
security page is not collection success.
