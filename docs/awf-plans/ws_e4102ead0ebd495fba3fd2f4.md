# Issue 306 Implementation Plan

## Goal

Resolve GitHub issue #306: adopted PR monitors must treat operator-declared `owned_paths` as editable even when they match protected quality-gate patterns, while separately surfacing GitHub token push failures for workflow files that require the `workflow` scope.

## Current Findings

- `find_protected_quality_gate_changes(...)` already skips paths covered by `owned_paths`, so the final protected-scope violation decision has the right concept of ownership.
- `diff_classified_protected_paths(...)`, `protected_file_diffs_for_committed_paths(...)`, and `_protected_file_diffs_for_status_paths(...)` gather protected diff evidence without `owned_paths`, so owned protected paths are still handled like generic protected paths in supporting logic.
- `monitor_prompts.py` has a static protected-file policy. It mentions owned paths generically but does not render the actual operator-owned protected paths into the repair prompt, so a repair agent can still emit a generic protected-file `NEEDS_HUMAN` or `DEFER` for an owned workflow file.
- `remote_ops._git_push_result(...)` treats non-divergence push failures as generic git push failures. There is no specific detection for GitHub workflow-file push rejection caused by a token missing the `workflow` scope.
- Comment repair push failures currently clear publish-dependent addressed state. For the missing-workflow-scope case, the correct behavior should preserve a merge-blocking `needs_human` state with the exact permission reason instead of allowing the feedback loop to become silent or generic.

## Intended Files And Modules To Touch

Tests first:

- `tests/unit/control/test_quality_gates_parts/test_quality_gates_part_001.py` or adjacent quality-gate part file: regression for owned workflow paths being excluded from protected diff classification while unowned workflow and pyproject paths still require semantic diff evidence.
- `tests/unit/control/test_protected_file_diffs.py`: regression for `protected_file_diffs_for_committed_paths(..., owned_paths=...)` not loading old/new content for an owned workflow path.
- `tests/unit/runtime/test_pr_monitor_runner_parts/test_pr_monitor_runner_part_001.py`: regression for `_protected_file_diffs_for_status_paths(..., owned_paths=...)` and protected-scope status validation with `.github/workflows/publish.yml` owned.
- `tests/unit/runtime/test_monitor_prompts.py`: regression that prompts render declared owned protected paths as editable and do not instruct generic protected-file approval for those owned paths.
- `tests/unit/runtime/test_pr_monitor_runner_parts/test_pr_monitor_runner_part_004.py` or another existing push/fix-cycle part: regression for GitHub missing-`workflow`-scope push stderr mapping to a specific reason code, clear permission message, and `needs_human` handling rather than `defer` or generic push failure.
- If needed for end-to-end monitor decision coverage, add or extend a focused loop/fix-cycle test asserting an unresolved review item becomes merge-blocking `needs_human` after the workflow-scope push blocker and is not auto-resolved.

Implementation files likely to change:

- `src/awf/control/quality_gates.py`: add an optional `owned_paths` parameter to `diff_classified_protected_paths(...)`, preserving default behavior for existing callers; exclude owned paths from the diff-classified protected list.
- `src/awf/control/protected_file_diffs.py`: add optional `owned_paths` to `protected_file_diffs_for_committed_paths(...)` and pass it through to the classifier.
- `src/awf/runtime/pr_monitor_runner/remote_repair.py`: pass adopted workspace `owned_paths` into protected diff loading for dirty status, unpushed commits, and sync-base push validation; add `owned_paths` to `_protected_file_diffs_for_status_paths(...)`.
- `src/awf/runtime/monitor_prompts.py`: replace the static protected-file policy string with a helper that renders declared owned protected paths and makes the rule explicit: owned protected paths are editable, unowned protected paths require `NEEDS_HUMAN`.
- `src/awf/runtime/pr_monitor_runner/comments.py` and `src/awf/runtime/pr_monitor_runner/ci_ops.py`: load/pass workspace `owned_paths` into thread, review-comment, and CI repair prompts. Prefer a small shared runner helper if it avoids duplication.
- `src/awf/runtime/pr_monitor_runner/remote_prompt_ops.py`: reuse or extend existing owned-path loading helpers for prompt sections if that is the cleanest existing pattern.
- `src/awf/runtime/pr_monitor_runner/remote_ops.py`: add a specific missing-workflow-scope detector for git push stderr and return a dedicated `_GitPushResult` reason code and exact permission message.
- `src/awf/runtime/pr_monitor_runner/fix_cycle.py` and possibly `src/awf/runtime/pr_monitor_runner/helpers.py` / `loop.py`: route the workflow-scope push blocker into merge-blocking `needs_human` state and operator notification, preserving the reason code in audit/operation evidence.
- `src/awf/runtime/pr_monitor_runner/constants.py`: add the new reason code constant if the existing constants module is the local pattern.

## Test-First Sequence

1. Add classifier/protected-diff tests that fail because `owned_paths` cannot currently be passed into diff-classified protected path loading.
2. Add prompt tests that fail because owned protected paths are not rendered as editable prompt context.
3. Add push failure tests that fail because missing `workflow` scope is currently a generic `_GIT_PUSH_FAILED_REASON`.
4. Add fix-cycle or loop regression proving the permission blocker becomes `needs_human`, blocks merge, and does not resolve the thread or degrade to `defer`.
5. Implement the smallest changes to pass those tests, keeping existing unowned protected-file behavior unchanged.
6. During implementation, create `plans/ISSUE_306_PLAN.md` before source/test edits and `plans/ISSUE_306_VALIDATION.md` after focused validation, per repo Plan-and-Validate rules. This planning phase may only write the configured AWF plan artifact, so those files are intentionally deferred.

## Validation Commands

Focused commands to run during implementation after the failing tests are written and code changes are made:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/control/test_protected_file_diffs.py tests/unit/control/test_quality_gates_parts/test_quality_gates_part_001.py -q
uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_monitor_prompts.py tests/unit/runtime/test_pr_monitor_runner_parts/test_pr_monitor_runner_part_001.py -q
uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_runner_parts/test_pr_monitor_runner_part_004.py -q
uv run --python 3.12 --extra dev pytest tests/unit/test_core_decomposition_maintainability.py -q
uv run --python 3.12 --extra dev ruff check src/awf/control/quality_gates.py src/awf/control/protected_file_diffs.py src/awf/runtime/monitor_prompts.py src/awf/runtime/pr_monitor_runner/comments.py src/awf/runtime/pr_monitor_runner/ci_ops.py src/awf/runtime/pr_monitor_runner/remote_ops.py src/awf/runtime/pr_monitor_runner/remote_repair.py tests/unit/control/test_protected_file_diffs.py tests/unit/control/test_quality_gates_parts/test_quality_gates_part_001.py tests/unit/runtime/test_monitor_prompts.py tests/unit/runtime/test_pr_monitor_runner_parts/test_pr_monitor_runner_part_001.py tests/unit/runtime/test_pr_monitor_runner_parts/test_pr_monitor_runner_part_004.py
uv run --python 3.12 --extra dev mypy src/awf/control/quality_gates.py src/awf/control/protected_file_diffs.py src/awf/runtime/monitor_prompts.py src/awf/runtime/pr_monitor_runner/comments.py src/awf/runtime/pr_monitor_runner/ci_ops.py src/awf/runtime/pr_monitor_runner/remote_ops.py src/awf/runtime/pr_monitor_runner/remote_repair.py
```

Do not run full-repository pytest, full coverage, full frontend builds, or CI-equivalent gates in the agent phase under the AWF workspace contract. Record in `plans/ISSUE_306_VALIDATION.md` that full AWF/GitHub validation is managed after agent completion.

## Risks And Mitigations

- Prompt signature churn can touch several call sites. Mitigate with optional `owned_paths=()` defaults and focused prompt tests.
- GitHub push stderr formats may vary between OAuth tokens, GitHub Apps, and PATs. Mitigate with a narrow detector that requires workflow-file context plus missing `workflow` scope or workflow permission wording, and keep generic push failure behavior otherwise.
- Marking a push blocker as `needs_human` must not accidentally resolve the review thread. Cover this with a fix-cycle regression that checks state and pending resolution behavior.
- `pr_monitor_runner` file-size guards are strict. If a touched runner module approaches the limit, split new helpers along the existing sibling-module pattern instead of growing the file significantly.
- Owned path matching must continue using existing `owned_paths_overlap(...)` semantics to avoid introducing a second ownership model.

## Assumptions

- The relevant adopted workspace persists `.github/workflows/publish.yml` in `workspace.owned_paths`; adoption storage itself is already covered by existing tests.
- Missing workflow scope is only reliably observable after `git push`; this plan does not depend on a proactive GitHub token-scope API.
- A workflow edit inside declared `owned_paths` remains subject to external GitHub push permissions. Ownership answers whether the repair agent may edit the path; it does not grant GitHub token capabilities.
- Broad validation and PR description wiring, including `Closes #306`, will be handled in the implementation/PR phase, not this planning artifact.

## Explicit Non-Goals

- Do not weaken protected-file policy for unowned workflows, pyproject, coverage, or CI configuration files.
- Do not change adoption semantics or owned-path normalization beyond passing existing ownership context into the protected-file and prompt surfaces.
- Do not add repo-specific Aira assumptions; `.github/workflows/**` and GitHub `workflow` scope handling must remain generic.
- Do not add secret/token logging or token introspection.
- Do not run broad local gates in this AWF agent phase.
