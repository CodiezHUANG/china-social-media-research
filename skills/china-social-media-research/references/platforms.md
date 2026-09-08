# Platform routing

Read only the section for the requested platform.

## Shared setup

All examples preview a command. Add `--execute` only after reviewing the plan
and authorizing the live collection. Use a new output directory for each run.

The examples below write `scripts/collect.py` for readability. Prefix it with the
resolved skill directory unless the shell is already in that directory:
`${CLAUDE_SKILL_DIR}/scripts/collect.py` in Claude Code, or
`skills/china-social-media-research/scripts/collect.py` from a repository
checkout. See the SKILL.md "Script paths" section.

## Xiaohongshu / Rednote

Backend: separately installed MediaCrawler, configured with
`MEDIACRAWLER_ROOT`.

```powershell
python scripts/collect.py --platform xhs --surface search `
  --query 'example film' --max-items 20 --comments-per-item 20 `
  --login qrcode --output '.\runs\xhs-pilot'

python scripts/collect.py --platform xhs --surface detail `
  --item-url 'https://www.xiaohongshu.com/explore/<NOTE_ID>?xsec_token=<TOKEN>&xsec_source=pc_search' `
  --max-items 1 --comments-per-item 20 --output '.\runs\xhs-detail'
```

Detail collection requires the complete public note URL, including
`xsec_token` and `xsec_source`. A bare note ID is rejected because the backend
cannot reliably retrieve the note from it.

Do not treat international-domain access as evidence that users are outside
China. Record the backend domain, account state, network location, query, and
capture time.

## Douyin

Backend: separately installed MediaCrawler, configured with
`MEDIACRAWLER_ROOT`.

```powershell
python scripts/collect.py --platform douyin --surface search `
  --query 'example film' --max-items 20 --comments-per-item 20 `
  --login qrcode --output '.\runs\douyin-pilot'
```

The collected comments are first-level only by default. Engagement counters
are time-specific platform values, not audience exposure.

## Weibo

Search uses MediaCrawler. Public comments and the current hot list use the
separately installed `weibo-cli`, configured with `WEIBO_CLI_BIN`.

```powershell
python scripts/collect.py --platform weibo --surface search `
  --query 'example film' --max-items 20 --comments-per-item 0 `
  --login qrcode --output '.\runs\weibo-search'

python scripts/collect.py --platform weibo --surface comments `
  --item-id '<PUBLIC_POST_ID>' --comments-per-item 20 `
  --output '.\runs\weibo-comments'

python scripts/collect.py --platform weibo --surface hot `
  --max-items 30 --output '.\runs\weibo-hot'
```

A hot-list file is one snapshot, not a historical time series. Missing from a
snapshot does not mean zero prior visibility.

## Bilibili

Backend: a separately installed and reviewed Bilibili collector configured
with `BILIBILI_CRAWLER_ROOT`. Search and detail/danmaku are deliberately split
so the BV sampling frame can be inspected between them. Set a private
`BILI_PSEUDONYM_SALT` in the local process environment before either stage.

```powershell
python scripts/collect.py --platform bilibili --surface search `
  --query 'example film' --max-items 20 --output '.\runs\bili-search'

python scripts/collect.py --platform bilibili --surface detail `
  --input-file '.\approved_bv.txt' --max-items 20 `
  --output '.\runs\bili-detail'
```

Verify every BV before the detail step. Comments are a visible first-page
sample. Danmaku are a capture-time snapshot, and an empty successful response
must be distinguished from a failed request.

The search frame is de-duplicated and truncated after collection so
`--max-items` remains a real upper bound. Search records include the capture
timestamp injected by the plan.

## Douban Movie

When `MEDIACRAWLER_ROOT` is configured, the adapter reuses that project's
Playwright-enabled virtual environment. Otherwise install the bundled adapter
dependency with `python -m pip install -r requirements.txt`, install Chromium
with `python -m playwright install chromium`, or set `DOUBAN_PYTHON` to an
existing Playwright-enabled interpreter.

The bundled Playwright adapter supports one verified movie subject. It caps a
run at five pages, does not spoof browser automation signals, and stops on an
access challenge.

```powershell
python scripts/collect.py --platform douban --surface reviews `
  --item-id '<SUBJECT_ID>' --max-pages 5 --output '.\runs\douban-reviews'

python scripts/collect.py --platform douban --surface wantsee `
  --item-id '<SUBJECT_ID>' --max-pages 5 --output '.\runs\douban-wantsee'
```

Verify the subject using title, year, and creator metadata before collection.
Short reviews and “want to watch” comments are different populations and must
remain separate.

## Maoyan Movie

Backend: separately installed MaoyanCrawler configured with
`MAOYAN_CRAWLER_ROOT`. The adapter uses its bounded tag-pool runner.

```powershell
# Set MAOYAN_PSEUDONYM_SALT privately in the current process first.
python scripts/collect.py --platform maoyan --surface reviews `
  --item-id '<MOVIE_ID>' --tags '0' --max-pages 5 `
  --output '.\runs\maoyan-reviews'
```

Tag pools can overlap and their reported totals must not be added. Keep the
salt private and reuse it only when author linkage across approved runs is
methodologically necessary. The external runner temporarily needs the salt in
memory; the bundled wrapper prevents that value from being serialized into its
checkpoint and performs a defensive scrub after the process returns.
