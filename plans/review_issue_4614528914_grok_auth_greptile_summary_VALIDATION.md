# Review Issue 4614528914 Grok Auth Greptile Summary Validation

Plan reference: `review_issue_4614528914_grok_auth_greptile_summary_PLAN.md`

## Requirement Status

- Inspect `_check_grok` and `_prepare_isolated_grok_auth` for strict `is_file()` guards:
  Complete. Both locations perform an explicit regular-file check on `auth.json` before claiming file auth or creating an isolated mount. `_check_grok` (provider_readiness.py:1252) does `auth_json = host_home / ".grok" / "auth.json"; if auth_json.is_file():`. `_prepare_isolated_grok_auth` (auth_mounts.py:395) does `if not (source_dir / "auth.json").is_file(): return ()`.
- Confirm grok path does not use the `_existing_credential_sources` (`.exists()`) helper:
  Complete. Grok readiness uses a direct `is_file()` check (unlike `_check_claude` / `_check_gemini` which call the helper for their dir+file candidates). This keeps the readiness decision in sync with the prepare guard that intentionally skips non-regular-files to avoid shipping host binaries.
- Read the dedicated non-file regression and preflight file-auth test + comments:
  Complete. `test_provider_readiness_grok_ignores_non_file_auth_json` (test_provider_readiness_part_002.py:306) explicitly creates a directory at `~/.grok/auth.json`, asserts that readiness reports `GROK_ENV_AUTH_PRESENT` (or MISSING) instead of `GROK_FILE_AUTH_PRESENT`, and contains the comment: "_check_grok must use is_file (to match _prepare_isolated_grok_auth) ... Regression test for GitHub PR review thread PRRT_kwDOSJAM6s6G0PEp." The preflight test `test_selected_grok_preflight_uses_file_auth_before_xai_api_key` (part_001.py:1006) also exercises file-before-env and redaction.
- Read mount filter/skip tests:
  Complete. `test_service_auth_mounts_grok_isolated_copy_filters_binaries` seeds `bin/` and `sessions/`, asserts they are absent from the per-ws copy while auth.json+config.toml are present. `test_service_auth_mounts_skip_grok_when_auth_json_missing` asserts no mount and no target dir when only config.toml exists (no auth.json).
- Run only narrow targeted commands and capture output:
  Complete. See Evidence below. All four grok tests + ruff on the precise four files passed.
- Create this plan before other edits:
  Complete. `plans/review_issue_4614528914_grok_auth_greptile_summary_PLAN.md` was written (and committed in sequence) before the validation doc.
- Create validation recording status + evidence + verdict:
  Complete. This document.
- If verification confirms mismatch resolved and bot conclusion holds, record FALSE POSITIVE and commit only plans/ artifacts:
  Complete. No source, test, or docker files were edited. Only the two plans/ protocol documents were added. Verdict recorded below.
- Print `AWF-VERDICT: FALSE POSITIVE: ...`:
  Will be the final stdout action after commit.
- State that full validation remains with AWF:
  Complete (this doc + plan).

## Evidence

### Files changed (only protocol artifacts)
- `plans/review_issue_4614528914_grok_auth_greptile_summary_PLAN.md`
- `plans/review_issue_4614528914_grok_auth_greptile_summary_VALIDATION.md`

No production code, no tests, no compose files, no docs outside plans/.

### Focused verification commands (narrowest surface)

```bash
uv run --python 3.12 --extra dev pytest tests/unit/service/test_provider_readiness_parts/test_provider_readiness_part_002.py -q -k "grok_file or grok_ignores" --tb=line
```
Result: `.. [100%] 2 passed, 63 deselected in 0.60s`

```bash
uv run --python 3.12 --extra dev pytest tests/unit/service/test_provider_readiness_parts/test_provider_readiness_part_001.py -q -k "grok_preflight" --tb=line
```
Result: `.... [100%] 4 passed, 35 deselected in 0.50s`

```bash
uv run --python 3.12 --extra dev pytest tests/unit/node/test_service_auth_mounts.py -q -k "grok" --tb=line
```
Result: `.. [100%] 2 passed, 21 deselected in 0.55s`

```bash
uv run --python 3.12 --extra dev ruff check src/awf/service/provider_readiness.py src/awf/node/auth_mounts.py tests/unit/service/test_provider_readiness_parts/test_provider_readiness_part_002.py tests/unit/node/test_service_auth_mounts.py
```
Result: `All checks passed!`

All commands were strictly limited to grok auth behavior. No coverage, no full-suite, no mypy (per "no path args" convention and narrowest-only rule), no service bootstrap, no docker commands.

### Rationale for verdict

The Greptile review-level summary is a positive architectural summary ("Safe to merge", 5/5, "design is sound", "all auth paths covered", "previously flagged issues were addressed"). The only "claim" that could be actionable is the restated mismatch between `is_file()` and `path.exists()`.

Direct code inspection refutes any open defect:
- Grok readiness deliberately does **not** use `_existing_credential_sources` (the exists()-based helper); it duplicates the exact `is_file()` predicate used by the prepare function.
- The regression test `test_provider_readiness_grok_ignores_non_file_auth_json` (added for a prior review thread) plus the mount skip test together make the contract explicit and protected: a non-file at auth.json must never produce `GROK_FILE_AUTH_PRESENT` or an isolated mount.
- The bot's own table description of the change ("uses `_existing_credential_sources`") is stale wording (likely from an intermediate diff or LLM summary of the PR), but the bot explicitly qualifies it with "previously flagged issues were addressed in an earlier review cycle" and gives the merge-safe verdict.

Per the decision tree in the task prompt and monitor_prompts.py contract:
- Reviewer is not pointing at a live defect (the fix they reference is present + tested).
- This is "stale, or pure review boilerplate" (positive bot summary after prior cycle closed the flag).
- Therefore: do not change code. Record as FALSE POSITIVE.

This matches "If the feedback is wrong, stale, or pure review boilerplate, do not change code. ... print `AWF-VERDICT: FALSE POSITIVE: <one-sentence reason>` ... Do not write any PR comment for review-level verdict bookkeeping."

Full AWF/GitHub validation, the 99% coverage gate, OpenAPI drift, console checks, and merge policy are owned by AWF (and the PR monitor) after this agent phase per the workspace contract and .awf/workspace.yml. Only the narrow evidence above was collected locally.

## Iteration Notes

N/A — single pass verification against the saved plan; all checklist items satisfied with no gaps. No source changes were required, so no follow-up iteration.

## Commit

The commit (created after this validation) contains only the plan + validation under `plans/`:

```
fix: address review comment issue:4614528914 — greptile grok auth summary positive; prior mismatch resolved, no code change
```
