# Review Issue 4614528914 Grok Auth Greptile Summary Plan

## Problem Statement and Scope

A review-level (outside-diff) comment `issue:4614528914` from greptile-apps on PR #378 (dimileeh/aira-agent-workspace-fabric) provides a positive Greptile Summary of the Grok OAuth file-auth + filtered per-workspace copy feature.

The summary states "Safe to merge — the binary-filtering design is sound and all auth paths are covered by unit tests." (confidence 5/5). It notes that the file-auth guard uses `is_file()` (strict) in prepare, the copy allows only auth.json/config.toml, tests confirm bin/ and sessions/ are excluded. It acknowledges "previously flagged issues" (the .exists() vs is_file() mismatch between readiness check and prepare) "were addressed in an earlier review cycle." The "Important Files Changed" table still describes provider_readiness.py as using `_existing_credential_sources` (path.exists()) for grok — wording that appears stale relative to the final code.

This task is to address the review-level comment per the AWF workspace contract and decision tree for review comments:
- Verify the claim (mismatch addressed?) directly against current code and tests.
- Produce only a FALSE POSITIVE verdict (no code change) if the bot's own conclusion ("addressed", "safe to merge") is correct and no latent defect remains.
- Do not edit source, tests, docker yml, or protected files.
- Record investigation via plan + validation only (per PLAN_EXECUTION_PROTOCOL).
- Use narrowest focused checks only; state that full AWF/GitHub validation is post-agent.
- Commit locally with message referencing the comment id.
- Print `AWF-VERDICT: FALSE POSITIVE: ...` (no GitHub PR comment).

Scope is strictly verification + plan/validation artifacts for this review comment. No behavior changes, no refactors, no new helpers, no weakening of existing grok regression tests.

Out of scope:
- Any edit to src/awf/service/provider_readiness*.py, src/awf/node/auth_mounts.py, tests, or docker/compose/local-service.yml.
- Running broad pytest, --cov, full ruff/mypy, console builds, or CI-equivalent commands.
- Branch ops, push, or PR interactions (AWF-owned).
- Changes to planning docs under docs/awf-plans (this is a plans/ protocol artifact for the review task).

## Requirements Checklist

- [ ] Inspect `_check_grok` (provider_readiness.py) and `_prepare_isolated_grok_auth` (auth_mounts.py) to confirm both perform a strict `auth.json.is_file()` guard (no `.exists()` path for grok).
- [ ] Confirm `_check_grok` does not call `_existing_credential_sources` (the exists()-based helper used for claude/gemini); grok uses direct is_file to match the prepare strictness.
- [ ] Read the dedicated regression `test_provider_readiness_grok_ignores_non_file_auth_json` (and its comment referencing prior review thread) and the preflight file-auth precedence test; confirm they protect the is_file contract.
- [ ] Read mount tests confirming `bin/` and `sessions/` are never copied when auth.json present, and mount is skipped when auth.json absent.
- [ ] Run only the narrowest targeted commands (specific -k grok tests in the two part files + ruff on exactly the 4 files) and capture output as evidence.
- [ ] Create this plan file under `plans/` first (before further steps that could be seen as "coding").
- [ ] Create `plans/review_issue_4614528914_grok_auth_greptile_summary_VALIDATION.md` recording per-requirement status + exact command evidence + explicit verdict rationale.
- [ ] If verification shows the mismatch is resolved and bot conclusion holds, treat as FALSE POSITIVE (no source change); commit only the two plan artifacts.
- [ ] Print the exact `AWF-VERDICT: FALSE POSITIVE: <one-sentence reason>` to stdout at end.
- [ ] State explicitly in validation that full AWF/GitHub validation, coverage gate, and merge gating are owned by AWF after this agent phase.

## Implementation Steps

1. (Already performed in investigation pass) Use read_file + grep to locate the exact grok guards, helper usage, and test comments/assertions. Capture that _check_grok uses `if auth_json.is_file():` directly and prepare uses `(source_dir / "auth.json").is_file()`.
2. Write this plan file to `plans/review_issue_4614528914_grok_auth_greptile_summary_PLAN.md` (satisfies "save before coding").
3. Execute the narrow verification commands listed below (pytest -k on grok cases only; ruff on the precise touched test+src files). Capture that they pass.
4. Write the sibling VALIDATION.md documenting checklist status, exact outputs, and rationale for FALSE POSITIVE (the review comment is a positive bot summary whose noted "previously flagged" mismatch is already covered by the is_file() + dedicated regression; the table wording in the summary is stale descriptive text, not an open defect).
5. `git add` only the two new plans/ files.
6. `git commit` with message "fix: address review comment issue:4614528914 — greptile grok auth summary positive; prior mismatch resolved, no code change".
7. Print the AWF-VERDICT line as the terminal action of this task.

## Verification Commands and Pass Criteria

Narrow red/green evidence (run before/after where applicable, but here for confirmation only; no source change so all green):

```bash
uv run --python 3.12 --extra dev pytest tests/unit/service/test_provider_readiness_parts/test_provider_readiness_part_002.py -q -k "grok_file or grok_ignores" --tb=line
uv run --python 3.12 --extra dev pytest tests/unit/service/test_provider_readiness_parts/test_provider_readiness_part_001.py -q -k "grok_preflight" --tb=line
uv run --python 3.12 --extra dev pytest tests/unit/node/test_service_auth_mounts.py -q -k "grok" --tb=line
uv run --python 3.12 --extra dev ruff check src/awf/service/provider_readiness.py src/awf/node/auth_mounts.py tests/unit/service/test_provider_readiness_parts/test_provider_readiness_part_002.py tests/unit/node/test_service_auth_mounts.py
```

Pass criteria: the four grok-specific tests (file-present precedence, ignores-non-file, preflight file-before-env, mount filter+skip) pass; ruff clean on exactly those files. No other test modules, no coverage, no full lint suite.

Full AWF/GitHub validation (including 99% coverage gate, OpenAPI drift, console, ci-required) is intentionally not executed here per workspace contract; AWF owns broad validation, provenance, logs, and merge gating after agent completion.

## Assumptions / Changes

- The review comment being "addressed" means performing the mandated verification + verdict bookkeeping; because the bot itself declares the work safe and prior flags addressed, the correct outcome per decision tree is FALSE POSITIVE with zero source edits.
- No update to any ws_*.md conformance under docs/awf-plans is required for this review-address protocol task (those are AWF-generated per-workspace run artifacts).

## Post-Plan Iteration Note (added during execution)

After the initial plan+validation were committed (80064ab7), a merge from development (b1fb4485, the commit referenced by the Greptile summary as "last reviewed") was performed on the workspace branch. This merge refactored provider_readiness (moved helpers, added check_single_provider_readiness + public factories, shifted some line numbers) but left the grok _check_grok is_file() path, _prepare_isolated_grok_auth, auth files constant, and the dedicated grok regression tests in the part_*.py files untouched.

Per PLAN_EXECUTION_PROTOCOL §4, an iteration was performed:
- Re-inspected code post-merge (no behavior change to the guarded grok file-auth).
- Re-ran the exact narrow verification commands listed in this plan (results still green; see sibling VALIDATION Iteration 2 for fresh outputs and line number corrections).
- Updated only the VALIDATION.md (added Iteration 2 section, refreshed evidence, corrected stale line refs and test descriptions for accuracy). PLAN itself required only this note.
- Committed the validation update with a follow-up conventional commit also referencing issue:4614528914 (so the thread's bookkeeping is current for the reviewed merge commit).
- Still no source/test/docker edits; verdict remains FALSE POSITIVE.

This keeps the artifacts accurate for the review comment that was (re)triggered against the post-merge state without violating scope or contract rules on broad validation.
