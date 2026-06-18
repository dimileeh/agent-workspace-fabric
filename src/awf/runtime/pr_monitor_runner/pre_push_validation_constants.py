"""Shared pre-push validation reason-code constants."""

_PRE_PUSH_VALIDATION_FAILED_REASON = "PRE_PUSH_VALIDATION_FAILED"

_PRE_PUSH_VALIDATION_INFRASTRUCTURE_FAILED_REASON = "PRE_PUSH_VALIDATION_INFRASTRUCTURE_FAILED"

_PRE_PUSH_VALIDATION_FIX_FAILED_REASON = "PRE_PUSH_VALIDATION_FIX_FAILED"

_PRE_PUSH_VALIDATION_ROLLBACK_FAILED_REASON = "PRE_PUSH_VALIDATION_ROLLBACK_FAILED"
_PRE_PUSH_VALIDATION_TOOLCHAIN_MISSING_REASON = "PRE_PUSH_VALIDATION_TOOLCHAIN_MISSING"

_PRE_PUSH_VALIDATION_REPARENT_FAILED_REASON = "PRE_PUSH_VALIDATION_REPARENT_FAILED"

# The pre-push dirty finalize committed a path outside the operation's owned
# delta. The ownership gate in ``_try_finalize_pre_push_dirty_repair_state`` is
# checked *before* calling ``_commit_dirty_worktree``, but that sink runs a fresh
# ``git status``, may invoke protected-scope repair (which runs the agent CLI),
# and then stages all non-ignored dirty paths. A side effect between the gate
# check and the fresh staging scan can introduce an extra path outside
# ``owned_delta_paths``; the post-commit re-validation catches that and fails
# closed so the unowned commit is never silently pushed (review thread
# ``PRRT_kwDOSJAM6s6KZP8f``).
_PRE_PUSH_DIRTY_FINALIZE_UNOWNED_DELTA_REASON = "PRE_PUSH_DIRTY_FINALIZE_UNOWNED_DELTA"
