# Product design

China Social Media Research provides bounded collection across six platforms.
Its shared workflow owns task selection, limits, sessions, normalized records,
execution, partial results and diagnostics. Users select the platforms and
surfaces needed for their research rather than configuring every platform.

## Components

| Component | Responsibility | Runtime |
|---|---|---|
| CLI and plans | Task options, previews, budgets and platform routing | Python standard library |
| Douban | Review-page requests, HTML parsing, deduplication and export | Python standard library |
| Bilibili | Search, metadata, main/nested comments, XML snapshots, audit and supplement | Python standard library |
| Weibo | Hot snapshots, keyword search and cursor-paginated comments | Python standard library; Playwright for an explicit visitor session |
| Maoyan | Tag-view selection, offset paging, cross-view deduplication and export cap | Python standard library |
| Douyin | HTTP search and browser detail-response capture | Python standard library; Playwright for detail |
| Xiaohongshu | Browser search/detail, response/page-state parsing and regional host choice | Playwright |
| Sessions | User-operated login, platform-scoped state, atomic storage and reuse | Playwright for login |
| Execution and privacy | Child-environment filtering, salted pseudonyms, manifests and cancellation | Python standard library |
| Host adapters | Shared entry points for Codex and Claude Code | Host plugin formats |

Playwright and browser engines are declared upstream dependencies under their
own licenses. Install them only for selected browser operations. Use the
checkout's own Python environment; an explicit DOUBAN_PYTHON can select the
Douban interpreter. Dependency binaries must match the chosen interpreter.

## Task and session boundaries

Each collection task selects one platform and one surface. Lazy imports,
catalog filtering and offline preflight keep unselected dependencies separate.
Douban, Bilibili, Weibo, Douyin and Xiaohongshu have independent session
profiles. Maoyan is anonymous. Login is user-operated; stored state does not
establish that the session is currently valid online.

JSON workflows combine explicit tasks and execute them sequentially. Every
task retains its own budgets, credential scope, output and status. Default
policy stops on a failure; explicit continuation proceeds on other platforms
while skipping later tasks on the failed platform. Search results do not
silently become approved inputs for subsequent detail collection.

## Records and interpretation

Records use shared schema 0.2 with platform/surface, public object ID, text,
timestamps, pseudonym and platform-specific fields. Sampling contracts remain
surface-specific: a first-page comment sample, a tag view and an XML danmaku
snapshot do not imply complete platform populations. Failed, cancelled,
unattempted and completed work must remain distinguishable.

Plans and manifests contain credential variable names, never their values.
Private session state is outside the checkout. Run outputs may contain
research text and stay outside published artifacts.

See [architecture](PLATFORM_ARCHITECTURE.md) and [workflows](WORKFLOWS.md).
