# China Social Media Research

A Python research collection tool with a shared CLI and plugins for Codex and
Claude Code. Platform collectors share session handling, canonical records,
bounded execution and manifests through one collection workflow.

The tool covers six platforms and 14 collection surfaces. A route is not a
promise of complete or continuous platform access.

## Install

Create an independent Python 3.10+ environment in this checkout:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
.\.venv\Scripts\cmc.exe --version
.\.venv\Scripts\cmc.exe platforms
```

Install the browser extra only for platforms whose route uses
Playwright:

```powershell
.\.venv\Scripts\python.exe -m pip install -e '.[browser]'
.\.venv\Scripts\python.exe -m playwright install chromium
```

### Skill and plugin runtime

The skill directory is an entry-point layer, not a self-contained copy of the
collector. The bundled scripts resolve `china_media_crawler` in this order:

1. `CMC_ROOT/src`, when `CMC_ROOT` points to a full checkout.
2. The checkout's `src` directory, when the full repository is present.
3. The installed `china-media-crawler` package in the active Python environment.

A checkout works without installing the package. If only the skill subdirectory
is copied into a host, install the core package in the Python interpreter that
host uses, or set `CMC_ROOT` to a full checkout. A copied skill subdirectory
alone does not contain the collector and exits with an actionable message.

### Claude Code

```
/plugin marketplace add CodiezHUANG/china-social-media-research
/plugin install china-social-media-research@codiez-research-plugins
```

The marketplace installs the whole repository, so the bundled scripts find the
collector in the plugin's own `src` directory without a separate package
install. The plugin adds the skill and two slash commands:

- `/csmr-preflight [platform]` — offline dependency check, no network request.
- `/csmr-plan [platform] [surface] [query or id]` — preview a bounded plan. It
  never collects; a live run stays a separate, explicitly approved step.

### Codex

Install this repository as a Codex skill plugin; it is discovered through
`.codex-plugin/plugin.json`.

## Commands

```powershell
cmc preflight --platform douban
cmc platforms --json
cmc plan --platform bilibili --query 'example film' --max-items 20 --output '.\runs\bili' --purpose 'Reception pilot'
cmc collect --platform bilibili --query 'example film' --output '.\runs\bili' --purpose 'Reception pilot' --execute
cmc workflow --file examples/selected-platforms.json --output runs/selected-pilot
```

`collect` previews by default. A live run requires `--execute`; browser-based
collection runs headless. `plan` rejects `--execute`. The optional `--purpose`
records a single-line research purpose in the plan and in `run_manifest.json`.

## Platforms

| Platform | Surfaces | Transport | Authentication | Required salt |
|---|---|---|---|---|
| Bilibili | search, detail, comments, danmaku | stdlib HTTP | anonymous / optional saved session | `BILI_PSEUDONYM_SALT` |
| Douban | reviews, wantsee | stdlib HTTP | saved session or Cookie | optional `DOUBAN_PSEUDONYM_SALT` |
| Maoyan | reviews | stdlib HTTP | anonymous | `MAOYAN_PSEUDONYM_SALT` |
| Weibo | search, comments, hot | stdlib HTTP | anonymous / saved session / visitor | `WEIBO_PSEUDONYM_SALT` for search/comments; none for hot |
| Douyin | search, detail | HTTP search; Playwright detail | saved session | `DOUYIN_PSEUDONYM_SALT` |
| Xiaohongshu | search, detail | Playwright | saved session | `XHS_PSEUDONYM_SALT` |

All collectors use explicit item/page budgets. Failures preserve partial data;
the default is to stop. Bilibili content also supports explicit per-video
continuation, returning a partial-failure status. See
[content contracts](docs/BILIBILI_CONTENT.md). Existing output directories
are never reused.

## Sessions

Manual login is user-operated and stored outside the checkout:

```powershell
cmc login --platform douban
cmc login --platform bilibili
cmc login --platform weibo
cmc login --platform douyin
cmc login --platform xhs
cmc login --platform xhs --login-url https://www.rednote.com/
cmc login --platform xhs --status
cmc login --platform xhs --clear
```

Collection uses `--session auto|required|off`. `auto` uses a saved session
when available; `required` fails before work when no session exists; `off`
forces anonymous collection where supported. Xiaohongshu sessions can contain
both `xiaohongshu.com` and `rednote.com`; use `--login-url` to start the
rednote login page, and the collector selects the host from the saved state.
The login is saved only after the page reports a non-guest account. Weibo may use `--visitor-session` for anonymous visitor
cookies. Credentials are never written to plans or manifests.

## Salts and privacy

Set platform-specific pseudonym salts privately in the process environment.
Raw author IDs and display names are not exported. Review text, post text,
comments and location fields are still user-generated research data and need
appropriate handling.

## Workflows

A workflow file selects explicit tasks and runs them sequentially. The default
stops on failure; `--on-error continue` continues other platforms while
skipping later tasks on the failed platform. Each task keeps its own output and
manifest.

The task format, limits and continuation rules are described in
[WORKFLOWS.md](docs/WORKFLOWS.md). Bilibili research runs additionally support
`cmc bili-audit` and preview-first `cmc bili-supplement` for layer checks and
explicit missing-layer/retry collection.

## Testing

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -t tests
```

The suite runs offline against synthetic fixtures.

## Safety boundary

The tool collects small, documented samples for non-commercial academic
research. Login is a manual step: run `cmc login`, sign in in the dedicated
browser, and later runs reuse that session. If a platform asks for login or
verification during a run, the run stops until you sign in again. The tool
does not solve CAPTCHAs, rotate accounts or bypass access controls.

Check each platform's user agreement before collecting, and get permission
where it requires one.

## Third-party dependencies

Playwright and Chromium are upstream dependencies for browser-based routes.
See [THIRD_PARTY.md](THIRD_PARTY.md). collector code is maintained in
this repository.
