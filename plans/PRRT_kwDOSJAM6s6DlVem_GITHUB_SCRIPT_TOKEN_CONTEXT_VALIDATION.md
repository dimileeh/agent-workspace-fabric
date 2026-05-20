# PRRT_kwDOSJAM6s6DlVem GitHub Script Token Context Validation

Plan reference:
`plans/PRRT_kwDOSJAM6s6DlVem_GITHUB_SCRIPT_TOKEN_CONTEXT_PLAN.md`

## Requirement Status

- Complete: Added regression coverage proving comment-labeled
  `actions/github-script` steps that read `github.token` or `context.token` are
  blocked. Evidence: `tests/unit/control/test_quality_gates.py`.
- Complete: Existing safe GitHub comment scripts using non-sensitive context
  fields remain admitted. Evidence: focused `github_script` test selection
  passed after the fix.
- Complete: Existing blocks for unsafe APIs and process access remain intact.
  Evidence: existing unsafe `github-script` parametrizations passed with the new
  token-context cases.
- Complete: Kept the change scoped to this review thread. Evidence: changed
  only the quality-gate predicate, focused unit coverage, and this
  plan/validation pair.

## Verification Evidence

- Before production fix:
  `uv run --python 3.12 --extra dev pytest tests/unit/control/test_quality_gates.py -q -k "github_script"`
  failed on the new `github.token` and `context.token` regressions because zero
  violations were reported.
- After production fix:
  `uv run --python 3.12 --extra dev pytest tests/unit/control/test_quality_gates.py -q -k "github_script"`
  passed: `15 passed, 320 deselected`.
- `uv run --python 3.12 --extra dev ruff check src/awf/control/quality_gates.py tests/unit/control/test_quality_gates.py`
  passed.
- `uv run --python 3.12 --extra dev mypy src/awf/control/quality_gates.py`
  passed.

## Gaps

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_quality_gates.py -q`
  failed in three existing informational shell/job env-reference tests:
  `test_workflow_comment_continue_on_error_allows_safe_step_env_reference`,
  `test_added_informational_step_allows_safe_env_reference`, and
  `test_added_informational_job_allows_safe_job_env_reference`. Each reproduces
  in isolation and is outside this `actions/github-script` token-context fix.
