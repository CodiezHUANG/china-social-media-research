# Platform architecture

The product covers six platforms and 14 registered surfaces. Each collection
task selects one platform and one surface; a workflow combines explicit tasks.
Only selected routes are planned, checked and executed.

## Routing

`cmc platforms --json` exposes the registry. Every surface resolves to
one adapter and one transport label:

| Platform | Route | Transport |
|---|---|---|
| Bilibili search | `bilibili-public-search` | stdlib HTTP |
| Bilibili detail/comments/danmaku | `bilibili-public-content` | stdlib HTTP |
| Douban | `douban-public` | stdlib HTTP; Playwright only for login |
| Maoyan | `maoyan-public` | stdlib HTTP |
| Weibo | `weibo-public` | stdlib HTTP |
| Douyin | `douyin` | HTTP search; Playwright detail |
| Xiaohongshu | `xhs` | Playwright |

Catalog entries expose platform, surface, transport label and authentication
requirements. Each platform/surface selects one collector. Plans and manifests
use `backend` for its transport label. Failures do not silently switch routes.

## Sessions

Douban, Bilibili, Weibo, Douyin and Xiaohongshu share the private
browser-session lifecycle. Login is user-operated in a dedicated visible
browser; HTTP collection reuses scoped cookies without a browser, while
browser-based collection runs headlessly. Xiaohongshu state may
contain both `xiaohongshu.com` and `rednote.com`; the collector selects the host
from the state.

`--session auto|required|off` controls saved-state reuse. `auto` uses saved
state when present; `required` fails before work when absent; `off` forces
anonymous collection where supported. Weibo additionally supports an explicit
anonymous visitor session.

## Dependencies

Preflight is offline and only checks the selected route:

- stdlib-routes: Python standard library plus the platform salt;
- Douban: standard library for collection; Playwright is only needed for
  manual login;
- Douyin detail and Xiaohongshu: Playwright plus salt;
- Weibo visitor session: Playwright only when explicitly requested.

Missing selected requirements are reported independently. Preflight never
logs in and never treats a passed dependency check as online acceptance. Manual
login for any of the five providers requires the optional browser dependency.

## Outputs

Every run writes `records.jsonl` (or a platform-specific JSONL name),
`*_manifest.json` and `run_manifest.json`. Manifests record limits, page/object
progress, stop reasons, safe failure metadata and partial results. Credentials,
salts and raw author identifiers are not serialized.

## Workflows

`cmc workflow --file tasks.json` previews or sequentially executes an explicit
subset of tasks. Default failure policy stops the workflow; explicit
continuation proceeds only on other platforms. Each task keeps its own output
directory and child manifest. See [workflow options](WORKFLOWS.md).
