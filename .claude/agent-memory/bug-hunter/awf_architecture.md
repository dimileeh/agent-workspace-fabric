---
name: AWF architecture snapshot
description: One-paragraph map of the AWF codebase so scans know where the pressure points live
type: project
---

AWF (Aira Workspace Fabric) orchestrates Claude Code / Codex / Gemini inside ephemeral Docker workspaces to execute tasks. Key layers:

- `src/awf/runtime/pr_monitor.py` — pure decision core (`decide()`), returns one `MonitorAction` per call.
- `src/awf/runtime/pr_monitor_runner.py` — I/O orchestrator wrapping `decide()`, handles gh calls, git, docker.
- `src/awf/control/executor.py` — drives `ready` → `monitoring_pr`/`completed`. Branch-drift recovery, orphan-history recovery.
- `src/awf/control/validation_fix_cycle.py` — validation retry loop, prompt builder.
- `src/awf/runtime/validation.py` — runs test_commands + optional alembic upgrade inside the agent container.
- `src/awf/node/git_manager.py` — bare mirror + per-workspace worktree plumbing.
- `scripts/run_awf.py` — one-shot driver; creates GitManager per-task (concurrency caveat).
- `scripts/release_pr_watcher.py`, `scripts/schedule_release_pr.py`, `scripts/attach_feature_pr_monitor.py` — process-spawning shells around `run_awf.py`.

**Why:** Recent PRs kept touching `pr_monitor*`, `executor.py`, `validation.py`, `git_manager.py`. Scans should start there.

**How to apply:** When asked to scan AWF, begin with the monitor + runner + executor + validation, then move to scripts that spawn `run_awf.py`. The state machine in `src/awf/control/state_machine.py` is the audit authority for status transitions.
