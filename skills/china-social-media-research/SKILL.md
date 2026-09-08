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

## Workflow

1. Confirm the platform, public query or object IDs, collection surface,
   maximum items, comment cap, output directory, and research purpose.
2. Run `<SKILL_DIR>/scripts/preflight.py --platform <platform>` without network
   access.
3. Preview `<SKILL_DIR>/scripts/collect.py` without `--execute`. Check the emitted
   command, limits, required environment-variable names, and output path.
4. Treat `--execute` as a separate live external action. Add
   `--allow-visible-browser` only after obtaining approval to launch a visible
   browser. Use `--headless` when no interactive login is needed.
5. Stop on login challenges, CAPTCHA, HTTP 403/412/429, unexpected HTML, schema
   changes, or repeated connection failures. Preserve partial output and do not
   respond by increasing concurrency, rotating accounts/proxies, or bypassing
   controls.
6. Validate returned counts, duplicates, timestamps, encoding, missing fields,
   and pseudonymization. Preserve the generated `run_manifest.json` beside the
   raw output.

## Default limits

- Pilot first: at most 20 posts/videos or five review pages.
- One crawler worker and no proxy.
- At most 20 first-level comments per item by default; never collect nested
  replies unless a separately documented protocol requires them.
- Create a new output directory for every run. Never overwrite or append to a
  prior raw collection.
- Every backend step has a 30-minute wall-clock limit by default. Change it
  with `--timeout-seconds` only when the approved protocol requires it.

## Credentials and personal data

Never ask the user to paste a Cookie, password, token, pseudonym salt, or
browser profile into chat. Authentication material may be supplied only in the
backend's documented process environment variable and must never be printed or
written to the run manifest. Only environment variables needed by the selected
platform are forwarded to its backend. Do not copy browser-data directories.

Minimize fields at collection time. Raw author identifiers, profile URLs, and
free text can be personal data even when publicly visible. Keep raw exports
access-controlled, pseudonymize identifiers before analysis, and do not publish
the corpus with this skill.

## Interpretation

Search and recommendation results are ranked, personalized, and capture-time
dependent. A collected count is not a platform total, exposure estimate,
unique-person count, viewer count, or ticket-purchase count. Treat each output
as a bounded interface snapshot and report query, surface, capture time,
limits, backend revision, and failures.
