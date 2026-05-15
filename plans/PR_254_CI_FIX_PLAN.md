# PR 254 CI Fix Plan

## Problem Statement And Scope

PR #254 fails the `python-full-coverage` GitHub Actions job. The reported
failures are limited to PR monitor adoption documentation/help-surface
contracts:

- `tests/unit/docs/test_pr_monitor_adoption_docs.py::test_runbook_documents_agent_default_idempotency_semantics`
- `tests/unit/cli/test_cli.py::TestWorkspaceAdoptPr::test_adopt_pr_help_exposes_model_and_effort_flags`

The scoped fix is to restore the documented model/effort default policy wording
and keep the `awf workspace adopt-pr --help` contract stable without disabling
or weakening tests.

## Requirements Checklist

- Reproduce the focused failures or document any locally non-reproducible CI
  failure.
- Preserve current adoption behavior and only adjust docs/tests/source where the
  failing contracts require it.
- Make `docs/PR_MONITOR_ADOPTION.md` contain the exact default/no-override
  policy semantics asserted by the docs regression.
- Keep `awf workspace adopt-pr --help` exposing `--model` and `--effort` under
  the relevant local/CI test conditions.
- Add a validation record in `plans/PR_254_CI_FIX_VALIDATION.md`.
- Commit the local fix with a conventional commit message and do not push.

## Implementation Steps

1. Inspect the failed CI logs and run the two focused failing tests locally.
2. Inspect the PR monitor adoption docs and `workspace adopt-pr` CLI definition.
3. Update the docs wording so the exact model/effort default policy phrase is
   present and clear.
4. If the CLI help failure reproduces under focused or CI-like local commands,
   adjust the CLI help surface without changing request payload behavior.
5. Re-run the focused tests and any narrow adjacent CLI/doc test commands needed
   to cover the failure mode.
6. Write validation evidence and commit the changes locally.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/docs/test_pr_monitor_adoption_docs.py::test_runbook_documents_agent_default_idempotency_semantics tests/unit/cli/test_cli.py::TestWorkspaceAdoptPr::test_adopt_pr_help_exposes_model_and_effort_flags -q`
  - Passes both reported regressions.
- If CLI help remains locally non-reproducible, run a CI-like narrow command for
  the CLI module or the adoption subset and record the result.
- `git status --short` shows only intentional plan, validation, docs, and
  source/test changes before commit.
