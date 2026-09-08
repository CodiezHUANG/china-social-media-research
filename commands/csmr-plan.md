---
description: Preview a bounded China Social Media Research collection plan. Never collects data.
argument-hint: "[platform] [surface] [query or public id]"
---

Build a **preview** collection plan for the `china-social-media-research` skill.

This command never collects anything. Do not add `--execute` here under any
circumstance; a live run is a separate, separately approved action.

1. Read `${CLAUDE_PLUGIN_ROOT}/skills/china-social-media-research/SKILL.md`, then
   read **only** the matching platform section of
   `${CLAUDE_PLUGIN_ROOT}/skills/china-social-media-research/references/platforms.md`.
2. Confirm with the user before running anything: platform, surface, public query
   or object IDs, maximum items, comment cap, a **new** output directory, and the
   research purpose. Do not invent values for any of these.
3. Run the planner with Bash:

```
python "${CLAUDE_PLUGIN_ROOT}/skills/china-social-media-research/scripts/collect.py" --platform <platform> --surface <surface> --output <new run directory> [platform-specific flags]
```

4. Show the emitted plan JSON and walk the user through: the exact backend command,
   the effective limits, the **names** of the environment variables the backend will
   need, the output path, and the timeout.
5. Close by stating plainly that nothing has been collected, and that a live run
   requires a separate invocation the user explicitly approves.

Never put a Cookie, token, password, or pseudonym salt on a command line, in chat,
or in the run manifest.
