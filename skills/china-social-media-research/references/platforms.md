# Platform reference

Use the bundled scripts or cmc CLI to preview collection; add --execute
for an authorized live run. Resolve <SKILL_DIR> as described in SKILL.md. Salts and cookies are private process-environment values and must not
be placed in commands or manifests.

## Weibo

Surfaces: `hot`, `comments`, `search`. Hot is anonymous; comments/search use
`WEIBO_PSEUDONYM_SALT` and may use a saved login or explicit
`--visitor-session`.

```powershell
python <SKILL_DIR>/scripts/login.py --platform weibo
python <SKILL_DIR>/scripts/collect.py --platform weibo --surface hot --max-items 30 `
  --output '.\runs\weibo-hot'
python <SKILL_DIR>/scripts/collect.py --platform weibo --surface search `
  --query 'example film' --session required --max-items 3 --max-pages 1 `
  --output '.\runs\weibo-search'
python <SKILL_DIR>/scripts/collect.py --platform weibo --surface comments `
  --item-id '<PUBLIC_POST_ID>' --session required --comments-per-item 20 `
  --max-pages 1 --output '.\runs\weibo-comments'
```

## Douyin

Surfaces: `search`, `detail`. Login is required. Search uses stdlib HTTP with
the saved cookie and `msToken`; detail uses our own Playwright response
interception.

```powershell
python <SKILL_DIR>/scripts/login.py --platform douyin
python <SKILL_DIR>/scripts/collect.py --platform douyin --surface search `
  --query 'example film' --session required --max-items 3 --max-pages 1 `
  --output '.\runs\douyin-search'
python <SKILL_DIR>/scripts/collect.py --platform douyin --surface detail `
  --item-id '<PUBLIC_AWEME_ID>' --session required `
  --max-items 1 --output '.\runs\douyin-detail'
```

Set `DOUYIN_PSEUDONYM_SALT`.

## Bilibili

Surfaces: `search`, `detail`, `comments`, `danmaku`. Uses stdlib HTTP and
`BILI_PSEUDONYM_SALT`; no browser or Cookie is required for collection.

```powershell
python <SKILL_DIR>/scripts/collect.py --platform bilibili --surface search `
  --query 'example film' --max-items 20 --max-pages 2 `
  --output '.\runs\bili-search'
python <SKILL_DIR>/scripts/collect.py --platform bilibili --surface detail `
  --item-id BV1000000001 --max-items 1 --output '.\runs\bili-detail'
python <SKILL_DIR>/scripts/collect.py --platform bilibili --surface comments `
  --item-id BV1000000001 --comments-mode first_page --comments-per-item 20 `
  --max-pages 1 --output '.\runs\bili-comments'
python <SKILL_DIR>/scripts/collect.py --platform bilibili --surface danmaku `
  --item-id BV1000000001 --max-parts 1 --danmaku-per-item 100 `
  --output '.\runs\bili-danmaku'
```

## Douban Movie

Surfaces: `reviews` (watched short comments), `wantsee` (want-to-watch
comments). Collection uses stdlib HTTP; Playwright is only for manual login.

```powershell
python <SKILL_DIR>/scripts/login.py --platform douban
python <SKILL_DIR>/scripts/collect.py --platform douban --surface reviews `
  --item-id '<SUBJECT_ID>' --session required --max-pages 2 --max-items 21 `
  --output '.\runs\douban-reviews'
python <SKILL_DIR>/scripts/collect.py --platform douban --surface wantsee `
  --item-id '<SUBJECT_ID>' --session required --max-pages 1 --max-items 3 `
  --output '.\runs\douban-wantsee'
```

Verify the subject before collection. `reviews` and `wantsee` are different
populations and must remain separate.

## Maoyan Movie

Surface: `reviews`. Anonymous stdlib HTTP with `MAOYAN_PSEUDONYM_SALT`.

```powershell
python <SKILL_DIR>/scripts/collect.py --platform maoyan --item-id '<MOVIE_ID>' `
  --tags '0,1' --max-items 20 --max-pages 2 --output '.\runs\maoyan'
```

## Xiaohongshu / Rednote

Surfaces: `search`, `detail`. Login is required. The saved session supports
both `xiaohongshu.com` and `rednote.com`; the host is selected automatically.
Collection uses our own Playwright context and response interception.

```powershell
python <SKILL_DIR>/scripts/login.py --platform xhs
python <SKILL_DIR>/scripts/collect.py --platform xhs --surface search `
  --query 'example film' --session required --max-items 3 --max-pages 1 `
  --output '.\runs\xhs-search'
python <SKILL_DIR>/scripts/collect.py --platform xhs --surface detail `
  --item-url 'https://www.xiaohongshu.com/explore/<NOTE_ID>?xsec_token=<TOKEN>&xsec_source=pc_search' `
  --session required --max-items 1 --output '.\runs\xhs-detail'
```

Set `XHS_PSEUDONYM_SALT`. Detail requires the complete public note URL with
`xsec_token` and `xsec_source`; a bare note ID is rejected.

## General limits

Use small, documented samples. Empty pages, page limits, item caps and
observed ends are distinct stop reasons. A failed run preserves completed
records and writes safe failure metadata. Do not retry around login, security
or verification pages.
