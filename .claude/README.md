# `.claude/` — Project knowledge for AI assistants

This directory holds project-level context for [Claude Code](https://claude.com/claude-code)
and other compatible AI coding assistants working in this repo. Everything checked in here
is project knowledge — bug-pattern memories, architecture notes, agent-specific guidance —
not personal config or credentials (those are excluded via `.gitignore`).

## What's here

- **`agent-memory/`** — Persistent memories that AI assistants load when working on the
  repo. Currently:
  - `bug-hunter/awf_architecture.md` — one-line context on AWF's main layers
  - `bug-hunter/awf_hotspots.md` — recurring bug patterns with file:line refs and
    "first place to check" hints

  These are written when a memory would save real time on a future session. They're
  read-on-relevance, not always-loaded. Updating one is part of normal review work —
  if a pattern stops being true, edit or delete the file.

- **`CLAUDE.md`** — When present at repo root, Claude Code loads this as standing
  instructions for every session. AWF's lives at the repo root (`./CLAUDE.md`),
  not here.

## What's NOT here

These are ignored via `.gitignore` and stay per-developer:

- `settings.local.json` — local Claude Code settings overrides
- `.credentials.json` — OAuth tokens
- `projects/`, `todos/`, `.session*` — session-scoped state

## If you're not using Claude Code

This directory is safe to ignore. Nothing in `.claude/` is required to build, test,
or run AWF. The memories are useful context if you're scanning the codebase for bugs
or architectural drift, regardless of which tool you read them with.

## Adding new project knowledge

If you write a memory that would help future contributors (yours or anyone else's),
add it under `agent-memory/<your-agent-or-role>/` and link it from that subdirectory's
`MEMORY.md` index. One-line description, then the body. Keep claims grounded in actual
file:line evidence — stale memories are worse than no memories.
