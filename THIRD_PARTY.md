# Third-party dependencies

Collection uses the Python standard library and, for browser-based routes,
the Playwright browser automation library.

| Dependency | Purpose | License observed during initial development |
|---|---|---|
| Playwright | Manual login for Douban/Bilibili/Weibo/Douyin/Xiaohongshu; explicit Weibo visitor sessions; Douyin detail; Xiaohongshu search/detail | Apache-2.0 |
| Chromium | Browser runtime installed through Playwright | Upstream browser license |

Collector logic and the shared workflow are maintained in this repository.

Playwright and browser engines are upstream software installed as declared
dependencies under their own licenses. License observations are not legal
advice and may become stale; re-check upstream licenses before distribution.
