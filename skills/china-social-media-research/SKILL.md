---
name: china-social-media-research
description: Plan and run small, reproducible academic collections of public movie-related posts, comments, reviews, metadata, and Bilibili danmaku from Xiaohongshu, Douyin, Weibo, Bilibili, Douban, or Maoyan. Use for bounded cross-platform research sampling and manifests; do not use for private data, access-control bypass, requests to expose credentials, commercial harvesting, or platform-wide crawling.
---

# China Social Media Research

Use this skill to turn a research question into a bounded six-platform
collection. Read only the relevant platform section in
[references/platforms.md](references/platforms.md), then use the bundled
scripts instead of composing ad-hoc crawler commands.

## Script paths

`scripts/` and `references/` sit beside this file. Resolve `<SKILL_DIR>` to that
directory before running anything. A bare `scripts/collect.py` is correct only
when the shell's working directory is already `<SKILL_DIR>`, which is not the
case for an installed plugin.

- Claude Code: `${CLAUDE_SKILL_DIR}` expands to `<SKILL_DIR>`.
- Codex or a plain checkout: `<SKILL_DIR>` is
  `skills/china-social-media-research` under the repository root.

The plugin scripts mirror the unified `cmc` CLI:

| Script | Purpose |
|---|---|
| `scripts/preflight.py` | Check only the selected platforms offline |
| `scripts/collect.py` | Preview or execute one bounded collection |
| `scripts/workflow.py` | Preview or execute a selected-task JSON file |
| `scripts/login.py` | Manage private sessions for Douban, Bilibili, Weibo, Douyin or Xiaohongshu |
| `scripts/platforms.py` | Inspect routes, transports and authentication contracts |
| `scripts/bili_audit.py` | Build per-video Bilibili canonical layers and quality audit |
| `scripts/bili_supplement.py` | Preview or execute missing-layer/retry Bilibili supplement |

All scripts accept `--help` and can be run from any working directory.

The scripts are entry points, not a self-contained collector. They resolve the
core package from `CMC_ROOT/src`, the surrounding full checkout's `src`, or the
active Python environment. A copied skill subdirectory without one of those
routes exits with an installation message. Install the core package with
`python -m pip install -e ".[browser]"` from a full checkout, or set `CMC_ROOT`
to that checkout.

## Workflow

1. Establish the platform, public query or object IDs, collection surface,
   maximum items, comment cap, output directory, and research purpose. Reuse
   authorization and values already supplied; ask only for missing requirements.
2. Run `<SKILL_DIR>/scripts/preflight.py --platform <platform>` without network
   access.
3. Preview `<SKILL_DIR>/scripts/collect.py` without `--execute`, passing the
   confirmed research purpose as `--purpose "<single line>"`; it is recorded in
   the plan and in `run_manifest.json`. Check the emitted command, limits,
   required environment-variable names, output path, and any `notes`.
4. Treat `--execute` as a live external action and execute within the user's
   authorized scope. Collection uses headless browser routes where applicable;
   interactive login is a separate user-operated step.
5. Stop on login challenges, CAPTCHA, HTTP 403/412/429, unexpected HTML, schema
   changes, or repeated connection failures. Preserve partial output and do not
   respond by increasing concurrency, rotating accounts/proxies, or bypassing
   controls.
6. Validate returned counts, duplicates, timestamps, encoding, missing fields,
   and pseudonymization. Preserve the generated `run_manifest.json` beside the
   raw output.

## Selected tasks

For one or two platforms, write an explicit JSON task file and preview it:

```powershell
python <SKILL_DIR>/scripts/workflow.py --file tasks.json --output runs/selected-pilot
```

The preview lists each task's route, limits, authentication contract and
required environment variable names. It creates no output and sends no request.
Add `--execute` only after review. Tasks run sequentially into separate
`<output>/<id>/` folders. The default `--on-error stop` marks remaining tasks
not attempted; `--on-error continue` continues other platforms while skipping
later tasks from the failed platform. Do not add execution fields, output paths,
credentials or environment values to the task file.

## sessions

Douban, Bilibili, Weibo, Douyin and Xiaohongshu can reuse private manual-login
sessions. Maoyan does not require login:

```powershell
python <SKILL_DIR>/scripts/login.py --platform douban --status
python <SKILL_DIR>/scripts/login.py --platform bilibili --status
python <SKILL_DIR>/scripts/login.py --platform xhs --login-url https://www.rednote.com/
```

`--status` is offline and only reports saved-state presence, not online
validity. Run the command without `--status` to open the dedicated visible
browser; the user completes login manually. For Xiaohongshu, `--login-url`
selects the China or Rednote login page. The state is saved only after the page
reports a non-guest account. Then use `--session auto`
(default), `--session required`, or `--session off` in collection plans. Saved
state stays outside the checkout and is never printed or copied into manifests.
Each platform has its own state profile; selected tasks do not require the
other platforms to be logged in.

## Default limits

- Pilot first: at most 20 posts/videos or five review pages.
- One crawler worker and no proxy.
- At most 20 main comments per item by default. Bilibili's `first_page` mode
  also exports the nested replies returned with that page, labelled by level
  and parent; `deep` collects main comments only. Keep this coverage explicit.
- Create a new output directory for every run. Never overwrite or append to a
  prior raw collection.
- Every collector step has a 30-minute wall-clock limit by default. Change it
  with `--timeout-seconds` only when the approved protocol requires it.

## Credentials and personal data

Never ask the user to paste a Cookie, password, token, pseudonym salt, or
browser profile into chat. Authentication material may be supplied only in the
collector's documented process environment variable and must never be printed or
written to the run manifest. Only environment variables needed by the selected
platform are forwarded to its collector. Do not copy browser-data directories.

Minimize fields at collection time. Raw author identifiers, profile URLs, and
free text can be personal data even when publicly visible. Keep raw exports
access-controlled, pseudonymize identifiers before analysis, and do not publish
the corpus with this skill.

## Interpretation

Search and recommendation results are ranked, personalized, and capture-time
dependent. A collected count is not a platform total, exposure estimate,
unique-person count, viewer count, or ticket-purchase count. Treat each output
as a bounded interface snapshot and report query, surface, capture time,
limits, transport, and failures.
