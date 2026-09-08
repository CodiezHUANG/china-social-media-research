# Third-party backends

This repository does not redistribute the backends below. Users install them
separately and are responsible for reviewing their current licenses, platform
terms, and research-ethics requirements.

| Backend | Purpose | License observed during initial development |
|---|---|---|
| NanmiCoder/MediaCrawler | Xiaohongshu, Douyin, and Weibo search/detail collection | Non-commercial learning/research license; redistribution rights are limited |
| tamnd/weibo-cli | Public Weibo post, comment, and hot-rank lookup | Apache-2.0 |
| Bright-Crest/Bilibili-Crawl-Analyze | Bilibili video, comment, and danmaku collection | No license file was found in the reviewed checkout; do not redistribute its code without permission |
| YIKAILucas/MaoyanCrawler | Maoyan public short reviews | Apache-2.0 |

The bundled `douban_reviews.py` is a new, bounded adapter based on ordinary
Playwright page navigation and DOM extraction. It does not include the
third-party `douban_short_comment` repository or any saved browser state.

License observations above are not legal advice and may become stale. Pin the
backend revision used by each study and re-check its repository before a
release.
