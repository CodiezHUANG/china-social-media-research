# China Social Media Research

A minimal skill-first plugin for planning and running bounded academic
collections from six Chinese media platforms. It installs in both Codex and
Claude Code from the same repository:

- Xiaohongshu / Rednote
- Douyin
- Weibo
- Bilibili
- Douban Movie
- Maoyan Movie

The plugin is an orchestration layer. It does not vendor third-party crawler
repositories, browser profiles, credentials, or collected data.

## Status

This is an early, conservative `0.1.0` build. Commands preview their execution
plan by default. A live collection occurs only when the caller adds
`--execute`, and browser-based login may still require a separate interactive
approval from the host environment. Visible browser runs must also add
`--allow-visible-browser`; backend steps time out after 30 minutes by default.

## Layout

```text
.codex-plugin/plugin.json        # Codex manifest
.claude-plugin/
  plugin.json                    # Claude Code manifest
  marketplace.json               # Claude Code distribution entry
commands/                        # Claude Code slash commands
  csmr-preflight.md
  csmr-plan.md
skills/china-social-media-research/
  SKILL.md
  scripts/
    bilibili_search_frame.py
    collect.py
    douban_reviews.py
    maoyan_runner.py
    preflight.py
  references/
    platforms.md
```

Both hosts read the same `skills/` directory. Only the manifests and the
Claude Code slash commands differ.

## Install

### Claude Code

```
/plugin marketplace add CodiezHUANG/china-social-media-research
/plugin install china-social-media-research@codiez-research-plugins
```

That adds the skill plus two slash commands:

- `/csmr-preflight [platform]` — offline backend check, no network request.
- `/csmr-plan [platform] [surface] [query]` — preview a bounded collection plan.
  It never collects; a live run stays a separate, explicitly approved step.

### Codex

Install the plugin directory as a Codex skill plugin. It is discovered through
`.codex-plugin/plugin.json`.

### Script paths

Scripts resolve relative to the skill directory, not your working directory.
In Claude Code use `${CLAUDE_SKILL_DIR}/scripts/...`; from a plain checkout use
`skills/china-social-media-research/scripts/...`.

## Configure external backends

The bundled scripts require Python 3.10 or newer. When `MEDIACRAWLER_ROOT` is
configured, the Douban adapter automatically reuses MediaCrawler's
Playwright-enabled virtual environment. For a standalone Douban setup instead:

```powershell
python -m pip install -r skills/china-social-media-research/requirements.txt
python -m playwright install chromium
```

Set only the backends needed for a run. The root, binary, and Python values are
filesystem paths. Pseudonym salts are private local values and must not be
committed or pasted into chat.

```powershell
$env:MEDIACRAWLER_ROOT = 'C:\path\to\MediaCrawler'
$env:WEIBO_CLI_BIN = 'C:\path\to\weibo.exe'
$env:BILIBILI_CRAWLER_ROOT = 'C:\path\to\Bilibili-Crawl-Analyze'
$env:MAOYAN_CRAWLER_ROOT = 'C:\path\to\MaoyanCrawler'
$env:DOUBAN_PYTHON = 'C:\path\to\a\playwright-enabled\python.exe' # optional override
```

Bilibili and Maoyan runs also require `BILI_PSEUDONYM_SALT` and
`MAOYAN_PSEUDONYM_SALT`, respectively. Supply them through an approved local
secret manager or the current process environment; do not use a value copied
from this repository.

Authentication material is read only from backend-specific process environment
variables. Never put a Cookie value in a command, config file, issue, log, or
research manifest.

## Preview a plan

```powershell
python skills/china-social-media-research/scripts/collect.py `
  --platform douyin --surface search --query 'example film' `
  --max-items 20 --comments-per-item 20 --output '.\runs\douyin-pilot'
```

Run `preflight.py --platform all` to check backend availability without making
network requests. See `references/platforms.md` for platform-specific examples
and interpretation limits.

The orchestrator forwards only a small platform-specific environment-variable
allowlist to each third-party backend. It does not place credential values in
the preview plan or run manifest.

## Safety boundary

The plugin is intended for small, documented, non-commercial research
collections of public material. It must not be used to collect private data,
evade login or verification controls, rotate accounts or proxies to defeat
limits, or perform indiscriminate platform-wide harvesting.

## Third-party software

External backends retain their own licenses and terms. Read
[`THIRD_PARTY.md`](THIRD_PARTY.md) before distribution or use.
