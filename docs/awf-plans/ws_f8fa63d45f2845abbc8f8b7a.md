# Plan — Fix #479: BitBucket auto-merge wedges on an unrestricted repo (no merge-strategy config)

Workspace: `ws_f8fa63d45f2845abbc8f8b7a`
Issue: #479 — `MERGE_METHOD_MISMATCH: attempted=none; effective_allowed=none` on a BitBucket
Cloud repo that has **no** explicit merge-strategy configuration.

## Problem statement / root cause (verified)

On a BitBucket Cloud PR for an *unrestricted* repo, `destination.branch` carries only
`{"name": "<base>"}` — both `merge_strategies` and `default_merge_strategy` are **absent**
(BitBucket only enumerates strategies on the PR when branch restrictions narrow them).

`effective_merge_strategies(ctx)` in
`src/awf/common/bitbucket_client_parsing.py:95-108` returns `None` when both fields are absent.
That flows through:

- `BitBucketClient.fetch_repo_merge_methods` (`bitbucket_client.py:636`):
  `map_bb_merge_methods(None) -> ()` → repo allows **no** methods.
- the merge loop (`runtime/pr_monitor_runner/merge_loop.py`) then computes
  `effective_methods == ()` → records `MERGE_METHOD_MISMATCH`
  (`attempted=none; effective_allowed=none`) → `NotifyHuman` loop, never merges.

But an unrestricted BitBucket Cloud repo allows **all three** native strategies by default:
`merge_commit`, `squash`, `fast_forward`. So "neither field present" means *unconstrained/all
allowed*, **not** *none allowed*.

## Fix (single resolution point, generic core)

In `effective_merge_strategies`, change **only** the final "neither present" branch: instead of
returning `None`, return BitBucket Cloud's standard default allowed set using the **BB-native
strategy names** so the existing `map_bb_merge_methods` / `bb_merge_strategy_for_method` mapping
and the neutral merge-method preference order keep working:

```python
# module-level, near _AWF_TO_BB_MERGE_STRATEGY
# BitBucket Cloud allows all three native strategies on a repo with no explicit
# merge-strategy configuration; an unrestricted PR enumerates none of them (#479).
_BB_CLOUD_DEFAULT_MERGE_STRATEGIES: tuple[str, ...] = ("merge_commit", "squash", "fast_forward")
```

```python
def effective_merge_strategies(ctx: _PRContext) -> list[str] | None:
    if ctx.merge_strategies:
        return ctx.merge_strategies            # explicit restriction — honor EXACTLY (unchanged)
    if ctx.default_merge_strategy is not None:
        return [ctx.default_merge_strategy]    # only default present — unchanged
    # Neither present → unrestricted repo → BitBucket Cloud's default allowed set,
    # NOT "no method allowed" (which wedged the merge gate, #479).
    return list(_BB_CLOUD_DEFAULT_MERGE_STRATEGIES)
```

Notes:
- Return a fresh `list(...)` (not the shared tuple-backed module constant) to preserve the
  `list[str] | None` contract and avoid any aliasing/mutation surprises.
- Keep the docstring; update its final sentence so it no longer claims `None` is returned when
  both fields are absent — it now documents the BB-Cloud default set.
- **Explicit-restriction and default-only paths are unchanged** (the two early returns).

### Cascade — `fetch_branch_pull_request_allowed_merge_methods` (must be handled)

`fetch_branch_pull_request_allowed_merge_methods` (`bitbucket_client.py:651-657`) also calls
`effective_merge_strategies` and returns `None` when it is `None`. After the fix, the
neither-present case returns the default three instead of `None`, so this method now returns
`("merge", "squash", "fast_forward")` rather than `None`.

This is **behavior-preserving for the merge outcome**: in `_effective_merge_methods`
(`merge_loop.py:113-126`), `repo_methods` and `branch_methods` are both the default three, their
intersection is the same three, and the preference order `("squash", "merge", "rebase",
"fast_forward")` still selects `squash` first. For a genuinely unrestricted repo BitBucket
accepts `squash`, so the merge proceeds. (Per-method fallback retry is BitBucket-side and out of
scope — see Non-goals.)

Consequence: the existing test
`tests/unit/common/test_bitbucket_client_parts/test_bitbucket_client_part_004.py:609`
(`test_fetch_branch_merge_methods_absent_returns_none`) asserts `methods is None` and **will now
fail**. It must be updated (see Tests) to assert the new default-three return. This is the only
existing test whose expectation changes.

## Intended files to touch

Implementation:
- `src/awf/common/bitbucket_client_parsing.py` — add `_BB_CLOUD_DEFAULT_MERGE_STRATEGIES`
  constant; change the neither-present branch of `effective_merge_strategies`; refresh the
  docstring. (No change to `map_bb_merge_methods`, `bb_merge_strategy_for_method`, the two
  consumers in `bitbucket_client.py`, the merge loop, or GitHub.)

Tests:
- `tests/unit/common/test_bitbucket_client_forge.py` — add direct pure-function unit tests for
  `effective_merge_strategies` (cases a/b/c) and the map-through assertion (case d).
- `tests/unit/common/test_bitbucket_client_parts/test_bitbucket_client_part_004.py` — add a
  client-level regression for `fetch_repo_merge_methods` on an unconstrained PR; **update** the
  now-stale `test_fetch_branch_merge_methods_absent_returns_none`.

No source changes outside `bitbucket_client_parsing.py`. No migrations, no config, no console.

## Tests to write first (strict TDD)

Write these and confirm they fail (red) before editing `effective_merge_strategies`.

In `test_bitbucket_client_forge.py` (import `effective_merge_strategies` and `_PRContext` from
`awf.common.bitbucket_client_parsing`; build a small `_PRContext` factory with the merge fields
set and the branch/sha fields as `None`):

- (a) `merge_strategies=None, default_merge_strategy=None` →
  `effective_merge_strategies(ctx) == ["merge_commit", "squash", "fast_forward"]` (NOT `None`).
  This is the #479 regression.
- (b) `merge_strategies=["squash"], default_merge_strategy=None` →
  `["squash"]` (explicit restriction honored, unchanged).
- (b′) belt-and-braces: `merge_strategies=["squash"], default_merge_strategy="merge_commit"` →
  `["squash"]` (explicit wins over default, unchanged).
- (c) `merge_strategies=None, default_merge_strategy="fast_forward"` →
  `["fast_forward"]` (default-only, unchanged).
- (d) map-through: `map_bb_merge_methods(effective_merge_strategies(ctx_neither))
  == ("merge", "squash", "fast_forward")` — confirms the defaulted BB-native names map to the
  neutral method names the merge-loop preference order selects from.

In `test_bitbucket_client_part_004.py` (mirror the existing merge-method tests at lines 535-617,
using `FakeBitBucket` + `pr_payload(...)` + `fetch_pr_status` to prime `_pr_context`):

- `test_fetch_repo_merge_methods_defaults_to_bb_cloud_set_when_unconstrained`:
  `_seed_fetch_status(fake, pr=pr_payload())` (neither field present) → after `fetch_pr_status`,
  `fetch_repo_merge_methods(repo=repo()) == ("merge", "squash", "fast_forward")`. The direct
  reproduction of #479 at the client boundary.
- Update `test_fetch_branch_merge_methods_absent_returns_none` → rename to
  `test_fetch_branch_merge_methods_absent_defaults_to_bb_cloud_set` and assert
  `methods == ("merge", "squash", "fast_forward")` (was `methods is None`). Keep the
  `pr_payload()` seed and the comment, updated to explain the BB-Cloud default.

Coverage: the new branch return is a single executed line exercised by (a) and the client-level
regression; no defensive/unreachable code is added, so no coverage exclusion is needed.

## Validation commands (focused during agent phase; full gate owned by AWF/CI)

Focused, for the files changed:
```bash
uv run --python 3.12 --extra dev pytest tests/unit/common/test_bitbucket_client_forge.py -q
uv run --python 3.12 --extra dev pytest \
  tests/unit/common/test_bitbucket_client_parts/test_bitbucket_client_part_004.py -q
uv run --python 3.12 --extra dev ruff check src/awf/common/bitbucket_client_parsing.py
uv run --python 3.12 --extra dev ruff format --check src/awf/common/bitbucket_client_parsing.py
uv run --python 3.12 --extra dev mypy
```
The full gate (whole-suite `ruff`, `ruff format --check`, `mypy`, `pytest`, 99% coverage,
OpenAPI drift) is run by AWF/GitHub CI after the agent phase — not executed here.

## Risks & mitigations

- **Cascade onto `fetch_branch_pull_request_allowed_merge_methods`** (returns the default three
  instead of `None`). Mitigated: merge outcome is identical (intersection with the same repo set,
  same preference winner); the one affected test is updated deliberately and documented above.
- **Mutable-default aliasing** if the module constant were returned directly. Mitigated by
  returning `list(_BB_CLOUD_DEFAULT_MERGE_STRATEGIES)`.
- **Over-broadening**: a repo that genuinely disallows `squash`/`merge_commit` would now have
  AWF attempt a method BitBucket rejects. Accepted by design — BitBucket's merge endpoint
  rejects a disallowed method and the existing blocker path notifies a human; for an
  unrestricted repo (the #479 case) the preferred method is accepted. The pre-fix behavior
  (`attempted=none`) never merged *anything*, so this is strictly better.
- **No GitHub impact**: the change is confined to the BitBucket parsing module; GitHub merge
  methods come from a different code path and are untouched.

## Assumptions

- BitBucket Cloud's default-allowed strategy set for an unrestricted repo is exactly
  `merge_commit`, `squash`, `fast_forward` (the three native strategy values already mapped by
  `_BB_TO_AWF_MERGE_METHOD`).
- `_PRContext` continues to store `merge_strategies=None`/`default_merge_strategy=None` for a PR
  whose `destination.branch` omits those keys (confirmed at `bitbucket_client.py:1044-1052`).

## Non-goals (explicit)

- The neutral merge-method preference order / `fast_forward` handling in `merge_loop.py`
  (already handled via #448) — untouched.
- The actual BitBucket `merge_pr` REST call (already implemented; the fix only ensures a method
  exists to attempt) and any per-method retry/fallback on BitBucket.
- GitHub merge-method resolution and any GitHub behavior.
- Changes to `map_bb_merge_methods`, `bb_merge_strategy_for_method`, or the two
  `BitBucketClient` consumer methods' control flow (they are unchanged aside from the value
  `effective_merge_strategies` now hands them).
