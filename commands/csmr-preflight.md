---
description: Check China Social Media Research dependencies offline, without making any network request.
argument-hint: "[xhs|douyin|weibo|bilibili|douban|maoyan|all]"
---

Run the offline dependency check for the `china-social-media-research` skill.

Run this with Bash, substituting `python3` if `python` is not on PATH. If no
platform argument was given, use `all`:

```
python "${CLAUDE_PLUGIN_ROOT}/skills/china-social-media-research/scripts/preflight.py" --platform $1
```

This makes no network request and starts no browser. Report, per platform:

- which required environment variables are unset — **name only, never a value**;
- which dependencies, entry points, or browser runtimes are missing;
- what the user must install or configure before a collection can be planned.

Stop after reporting. Preflight is a separate step from planning a collection;
do not run `collect.py` in the same turn.
