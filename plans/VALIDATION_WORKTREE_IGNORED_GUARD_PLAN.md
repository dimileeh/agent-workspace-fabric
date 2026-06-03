# Fix: validation-worktree guard must not police gitignored files — PLAN

**Severity:** P0 outage. AWF is unusable on any repo whose validation mutates ignored
runtime/cache files (every Python repo: `uv sync` + `ruff/mypy/pytest` rewrite `.venv/`,
`.mypy_cache/`, `.pytest_cache/`, `.ruff_cache/`, coverage, `__pycache__/`). All four
dogfood tasks failed with `infrastructure_failure / VALIDATION_WORKTREE_CLEANUP_FAILED`.

## Root cause (verified in code)

PR ~#349 added a snapshot+content-signature guard over *ignored* files. After validation,
`cleanup_validation_worktree_side_effects` (`validation_worktree.py:993–1026`) hashes every
pre-existing ignored file and **fatally fails on any content drift**:

```
VALIDATION_WORKTREE_CLEANUP_FAILED:
  AWF validation modified pre-existing ignored files and they cannot be
  safely restored: .mypy_cache/3.12/cache.db, .venv/bin/activate, ...
```

It also fatally fails on ignored-file **deletion** (`:960`, `:980`) and on cross-pass
ignored **drift** (`execution_validation.py:_setup_ignored_snapshot_drift`).

## The principle (locked decision)

> **git already declares what is disposable.** Anything matched by the repo's
> `.gitignore` never enters the commit/PR (the worktree is per-workspace and torn down),
> so AWF validation creating / modifying / deleting it is **always safe** — for Python,
> Go, Java, C++, any repo. The guard must concern itself **only** with tracked files and
> untracked-non-ignored files. No language-specific allowlist, no per-profile config, no
> content hashing.

**D-decision (locked):** *Fully ignore* — remove ALL ignored-file enforcement (snapshot,
signatures, cross-pass drift, modify-fail AND delete-fail). The guard acts only on
tracked + untracked-non-ignored files.

## Guard scope — before vs after

```
                         BEFORE (#349, broken)              AFTER (this fix)
tracked file changed     git restore                        git restore            (unchanged)
untracked, NOT ignored   delete (side-effect cleanup)       delete                 (unchanged)
ignored file CREATED     deleted unless in baseline snap    LEFT ALONE
ignored file MODIFIED    FATAL (signature drift)            LEFT ALONE
ignored file DELETED     FATAL (deleted_ignored_*)          LEFT ALONE
ignored under NEW root   could be deleted (pre-set gap)     LEFT ALONE (uses live git --ignored)
huge .venv tree          content-hashed every pass          never inspected         (perf win)
```

## Changes (3 source files; deletion-heavy)

### 1. `src/awf/runtime/validation_worktree.py`
- `check_validation_worktree_clean`: drop `capture_ignored_paths_snapshot` param and the
  snapshot/signature capture block (`629–655`). Keep the existing ignored-exclusion of the
  dirty determination (`ignore_all_ignored` path).
- `cleanup_validation_worktree_side_effects`: drop params
  `ignore_ignored_paths_snapshot`, `ignore_ignored_paths_snapshot_signatures`; **delete the
  entire `903–1032` block** (re-snapshot, signatures, `deleted_ignored_paths`,
  `deleted_ignored_roots`, `modified_snapshot_paths`, the snapshot-gated untracked extend).
  Keep: HEAD-unchanged verify, tracked-file `git restore` (`862–890`), untracked-non-ignored
  cleanup (`892–902`).
- **Correctness upgrade:** the cleanup's internal check (`:821`) and final verify (`:1117`)
  currently filter against the *pre-validation* ignored set (`ignore_ignored_paths`), which
  misses ignored files under a **new** root created during validation. Switch both to
  `ignore_all_ignored=True` so cleanup leaves alone whatever git **currently** reports as
  ignored. This also lets `ignore_ignored_paths` plumbing be dropped.
- Remove now-dead helpers: `_ignored_path_still_reported`, `_regular_file_metadata_signature`,
  `_hash_regular_file_contents`, `_ignored_path_signature`, `_snapshot_ignored_path_signatures`,
  `_ignored_signature_lookup_by_normalized_path`, the `_IgnoredPathSignature` type, and the
  `ignored_paths_snapshot*` fields on `ValidationWorktreeCheck` (+ all consumers).
- Add a short ASCII "guard scope" comment (the table above) above the cleanup function.

### 2. `src/awf/control/executor/execution_validation.py`
- Delete `_setup_ignored_snapshot_drift` (`96–135`) and the setup-snapshot tracking
  (`256–258`, `290–294`, `330–370`).
- `check_validation_worktree_clean` calls at `:284` and `:1400`: drop
  `capture_ignored_paths_snapshot=True`.
- Cleanup calls at `:411`, `:472`, `:521`: drop the snapshot/signature args (and
  `ignore_ignored_paths` if cleanup goes fully live-`--ignored`).

### 3. `src/awf/runtime/pr_monitor_runner/pre_push_validation.py`
- Drop `capture_ignored_paths_snapshot` param + snapshot args from the check wrapper
  (`560–575`) and `_pre_push_validation_cleanup` (`585+`).

Reason code `VALIDATION_WORKTREE_CLEANUP_FAILED` stays — it still fires for the legitimate
failures (tracked `git restore` failed, HEAD changed mid-validation).

## Test coverage diagram (target)

```
GUARD BEHAVIOR (cleanup_validation_worktree_side_effects + check)        TEST
[+] tracked file modified by validation
    ├── [★★★ KEEP]  restored via git restore — test_validation_worktree
    └── [★★★ KEEP]  restore fails -> CLEANUP_FAILED
[+] untracked NON-ignored file created
    └── [★★★ KEEP]  deleted as side-effect
[+] HEAD changed during validation
    └── [★★★ KEEP]  reset/fail path
[+] ignored file MODIFIED (.venv, .pytest_cache content churn)
    └── [GAP→ADD CRITICAL] cleanup SUCCEEDS (was the outage)  — regression
[+] ignored file DELETED (e.g. uv sync rebuilds .venv)
    └── [GAP→ADD CRITICAL] cleanup SUCCEEDS (was fatal)
[+] newly-created ignored file under an EXISTING ignored root
    └── [GAP→ADD] left alone, not deleted
[+] newly-created ignored file under a NEW ignored root
    └── [GAP→ADD] left alone (proves live --ignored, not pre-set)
[+] mixed: untracked-non-ignored created + ignored mutated
    └── [GAP→ADD] non-ignored cleaned, ignored untouched, cleanup ok
[-] OBSOLETE: signature-drift / modified_snapshot / deleted_ignored fatal tests
    └── remove from test_validation_worktree_ignored_cleanup.py et al.

COVERAGE TARGET: every guard branch + 5 new regression/edge tests; obsolete tests deleted.
```

Test files in blast radius (update/trim): `test_validation_worktree_ignored_cleanup.py`
(primary), `test_validation_worktree.py`, `test_validation_worktree_head_cleanup.py`,
`test_validation_worktree_result_edges.py`, `test_pr_monitor_pre_push_validation*.py`,
`test_executor_validation_*`.

## Failure modes (post-fix)

| Codepath | Realistic failure | Test? | Handled? | Visible? |
|---|---|---|---|---|
| tracked `git restore` | restore_ref missing / restore errors | yes | CLEANUP_FAILED | clear |
| untracked cleanup | rm of a side-effect file fails | yes | CLEANUP_FAILED | clear |
| HEAD changed | validation moved HEAD | yes | reset/fail | clear |
| ignored mutation | (now a no-op) | yes (regression) | n/a | n/a |

No critical gaps: every remaining guard branch has a test and a clear error.

## NOT in scope
- Profile-configurable allowlist / language-specific defaults — rejected; the live
  `.gitignore` is the source of truth, nothing to configure.
- Changing the pre-validation dirtiness check — it already treats ignored-only-dirty as
  clean; untouched.
- The deletion/restore logic for tracked + untracked-non-ignored files — unchanged.

## Parallelization
Sequential — one cohesive guard change across 3 tightly-coupled files; no worktree split.

## Rollout
P0 outage: land on `development` and rebuild local stacks (`awf service bootstrap`) before
re-launching any AWF-on-AWF workspaces, which would otherwise fail identically.

## Implementation Tasks
- [ ] **T1 (P0, human: ~1.5h / CC: ~15min)** — runtime — strip ignored-file enforcement from validation_worktree guard
  - Surfaced by: Root cause — `validation_worktree.py:993-1026` fatal on ignored drift
  - Files: `src/awf/runtime/validation_worktree.py`
  - Verify: `pytest tests/unit/runtime/test_validation_worktree_ignored_cleanup.py -q`
- [ ] **T2 (P0, human: ~45min / CC: ~8min)** — control — remove setup-snapshot/drift tracking + snapshot args at call sites
  - Surfaced by: Architecture — `execution_validation.py` snapshot plumbing
  - Files: `src/awf/control/executor/execution_validation.py`
  - Verify: `pytest tests/unit/control/test_executor_validation_fix_cycle.py -q`
- [ ] **T3 (P0, human: ~20min / CC: ~4min)** — runtime — drop snapshot args from pre-push validation wrappers
  - Surfaced by: call-site sweep — `pre_push_validation.py:560,585`
  - Files: `src/awf/runtime/pr_monitor_runner/pre_push_validation.py`
  - Verify: `pytest tests/unit/runtime/test_pr_monitor_pre_push_validation*.py -q`
- [ ] **T4 (P0, human: ~1.5h / CC: ~15min)** — tests — 5 regression/edge tests + delete obsolete signature/drift tests
  - Surfaced by: Test review — the outage had zero regression coverage
  - Files: `tests/unit/runtime/test_validation_worktree_ignored_cleanup.py` (+ blast-radius files)
  - Verify: full clean-env coverage run ≥99%, ruff/mypy green

## GSTACK REVIEW REPORT

| Review | Trigger | Why | Runs | Status | Findings |
|--------|---------|-----|------|--------|----------|
| CEO Review | `/plan-ceo-review` | Scope & strategy | 0 | — | n/a (P0 bug fix, no product scope) |
| Codex Review | `/codex review` | Independent 2nd opinion | 0 | — | not run |
| Eng Review | `/plan-eng-review` | Architecture & tests (required) | 1 | CLEAR | 1 root cause; design simplified from allowlist to "trust .gitignore"; 5 regression tests required |
| Design Review | `/plan-design-review` | UI/UX gaps | 0 | — | n/a (no UI) |
| DX Review | `/plan-devex-review` | Developer experience gaps | 0 | — | not run |

- **UNRESOLVED:** 0 — D-decision locked (fully ignore: drop snapshot/signature/modify/delete enforcement).
- **VERDICT:** ENG CLEARED — ready to implement. Deletion-heavy fix across 3 source files,
  0 new classes/abstractions; 5 new regression/edge tests, obsolete signature tests removed.
