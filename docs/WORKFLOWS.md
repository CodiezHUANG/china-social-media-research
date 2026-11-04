# Selected collection tasks

`cmc workflow` selects one or several platform/surface tasks from a JSON file.
It previews by default and uses the same bounded planners, authentication
validation and isolated child environments as `cmc collect`. It neither logs
in nor acquires credentials. Only the explicitly listed tasks are planned.
The bundled plugin entry point is
`skills/china-social-media-research/scripts/workflow.py`; it accepts the same
arguments from any working directory.

```powershell
cmc workflow --file examples/selected-platforms.json --output runs/selected-pilot
# Review the preview, replace the synthetic movie ID/query and set the
# selected platforms' private environment variables before collecting.
cmc workflow --file examples/selected-platforms.json --output runs/selected-pilot --execute
```

The preview includes each task's collection route, budgets,
authentication contract and required environment variable names. It creates
no output directory, launches no process, reads no saved session and sends
no network request. Previewing is not a dependency or online-session validity
check; run the
selected route's `cmc preflight` separately when needed.

## Task file

The top-level object contains exactly `schema_version: "0.2"` and `tasks`,
an ordered list of 1..24 tasks. UTF-8 JSON is limited to 256 KiB; duplicate
JSON keys and unknown fields are rejected. See
[the example](../examples/selected-platforms.json). Task options are:

| Field | JSON type | Meaning |
|---|---|---|
| id | string, required | Unique output folder name: 1..48 lowercase ASCII letters/digits/`-`/`_`, starting with a letter; Windows device names excluded |
| platform | string, required | xhs, douyin, weibo, bilibili, douban or maoyan |
| surface | string | Same choices/defaults as collect |
| query, item_id, item_url | list of 1..200 strings | Explicit inputs; same platform validation as collect |
| input_file | string | UTF-8 approved-input file; relative paths resolve from the JSON file's directory |
| max_items, comments_per_item, max_pages, danmaku_per_item, max_parts, timeout_seconds | integer | Same bounded limits and defaults as collect; JSON booleans are not integers |
| tags, session, comments_mode, on_item_error | string | Same choices/defaults as collect; deep comments_mode and continue on_item_error are restricted to supported Bilibili routes |
| purpose | string | Single-line research purpose, at most 500 characters; recorded in the task's plan and manifests. Do not put credentials or personal data in it |
| visitor_session | boolean | Request an explicit Weibo visitor session |

Strings are single-line, non-empty and at most 4096 characters. Task files
do not accept shell commands, environment values, Cookies, passwords, output
paths, execution flags or browser approval. Keep private input URLs and
local configuration outside public example files. Ordinary plan/run
artifacts can contain approved input URLs and local paths; keep them private.

## Execution and failures

`--execute` runs sequentially in file order. All selected plans are
constructed and checked for required environment variables and output
configuration before reserving the workflow directory. Missing
configuration aborts the whole workflow without starting tasks. This check
does not validate network access, dependency imports or actual login validity.

The output must be new and must not traverse links/junctions. A task writes
to `<output>/<id>/` with its existing per-run and collector manifests.
`<output>/workflow_manifest.json` records plans, task status, exit codes,
start/end times and sanitized error types. It stores no credential values or
raw exception messages. Existing output is never reused; partial collection
files survive failures. There is no automatic resume or task-to-task input
discovery: review search results and explicitly approve downstream IDs.

| Policy | Behavior after a runtime task failure |
|---|---|
| --on-error stop (default) | Mark all remaining tasks not_attempted |
| --on-error continue | Continue other platforms; mark later tasks from the failed platform not_attempted with reason platform_failed |

The continuation policy never retries a failed task or switches endpoints.
Workflow `--on-error` applies between tasks/platforms. Bilibili `on_item_error`
applies between videos inside one task; `complete_with_failures` still returns
nonzero and therefore activates the workflow's failure policy. `comments_mode`
chooses first-page main/nested replies or explicit deep main-comment paging;
it does not expand the task's selected platform or inputs.

The workflow policy applies to any runtime failure, including a login challenge, access
restriction, timeout or connection error. Ctrl+C or a child cancellation
stops the entire workflow even in continue mode. Unexpected internal errors
are recorded and re-raised, with remaining tasks marked not attempted.
Exit status is 0 for complete success, 1 for recorded task failure, 130 for
cancellation; invalid configuration returns the CLI's standard error status.
