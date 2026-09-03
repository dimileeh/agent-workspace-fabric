"""Shared constants for AWF validation worktree reason codes."""

from __future__ import annotations

VALIDATION_WORKTREE_CLEANUP_FAILED = "VALIDATION_WORKTREE_CLEANUP_FAILED"
VALIDATION_WORKTREE_PRE_EXISTING_DIRTY = "VALIDATION_WORKTREE_PRE_EXISTING_DIRTY"
VALIDATION_WORKTREE_SIDE_EFFECTS_CLEANED = "VALIDATION_WORKTREE_SIDE_EFFECTS_CLEANED"
VALIDATION_WORKTREE_STATUS_FAILED = "VALIDATION_WORKTREE_STATUS_FAILED"
VALIDATION_INFRASTRUCTURE_ERROR = "VALIDATION_INFRASTRUCTURE_ERROR"

# Finite timeout for validation worktree Git probes so agent-poisoned local
# ``include.path`` / ``includeIf`` cannot hang executor validation or pre-push
# cleanliness checks (PRRT_kwDOSJAM6s6e-r1k).
VALIDATION_WORKTREE_GIT_TIMEOUT_SECONDS = 30.0

# AWF-agent-runtime artifact roots that reviewer subagents
# (bug-hunter/bug-validator/bug-judge) write into a worktree. Untracked
# artifacts under these roots never belong to the PR, so the dirty-tree
# guards suppress them unconditionally. Kept to EXACTLY .claude/agent-memory/
# — NOT all of .claude/ (settings etc. can be legitimate project files). The
# trailing slash is load-bearing: it makes both the `_is_under_ignored_path`
# matcher and a raw ``str.startswith`` correctly exclude the sibling
# ``.claude/agent-memory-archive/``.
AWF_AGENT_RUNTIME_IGNORED_ROOTS: tuple[str, ...] = (".claude/agent-memory/",)
