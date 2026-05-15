# PR 254 CI Fix Validation

Plan reference: `plans/PR_254_CI_FIX_PLAN.md`

## Requirement Status

- Reproduce the focused failures or document locally non-reproducible CI
  failure: Complete.
  - The docs regression failed locally before the fix.
  - The CLI help assertion passed locally in isolation and under `CI=true`; the
    GitHub Actions log remains the evidence for that prior CI-only failure.
- Preserve current adoption behavior and only adjust docs/tests/source where the
  failing contracts require it: Complete.
  - Changed only `docs/PR_MONITOR_ADOPTION.md` behavior text plus plan and
    validation records.
- Make `docs/PR_MONITOR_ADOPTION.md` contain the exact default/no-override
  policy semantics asserted by the docs regression: Complete.
  - The model/effort retry sentence now contains the exact asserted lowercase
    phrase.
- Keep `awf workspace adopt-pr --help` exposing `--model` and `--effort` under
  relevant local/CI test conditions: Complete.
  - The focused test and full CLI module passed under `CI=true`.
- Add validation record: Complete.
- Commit the local fix and do not push: Complete.
  - This validation record is included in the local commit; push remains owned
    by AWF.

## Evidence

Files changed:

- `docs/PR_MONITOR_ADOPTION.md`
- `plans/PR_254_CI_FIX_PLAN.md`
- `plans/PR_254_CI_FIX_VALIDATION.md`

Commands run:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/docs/test_pr_monitor_adoption_docs.py::test_runbook_documents_agent_default_idempotency_semantics tests/unit/cli/test_cli.py::TestWorkspaceAdoptPr::test_adopt_pr_help_exposes_model_and_effort_flags -q
```

Result: `2 passed in 1.78s`

```bash
CI=true uv run --python 3.12 --extra dev pytest tests/unit/cli/test_cli.py -q
```

Result: `102 passed in 5.47s`

```bash
CI=true uv run --python 3.12 --extra dev pytest -n 8 --dist=loadscope tests/unit/docs/test_pr_monitor_adoption_docs.py tests/unit/cli/test_cli.py::TestWorkspaceAdoptPr -q
```

Result: `15 passed in 29.89s`
