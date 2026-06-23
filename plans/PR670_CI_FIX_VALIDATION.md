# PR #670 CI failure repair validation

## Plan reference

- `plans/PR670_CI_FIX_PLAN.md`

## Requirement status

- Branch discipline / no broad CI: **Complete**
- Verdict parsing fix: **Complete**
- Line-limit split: **Complete**
- Targeted regression checks: **Complete**
- Validation report recorded: **Complete**
- Latest `python-full-coverage` near-miss diagnosed: **Complete**
- Meaningful parser coverage added for uncovered verdict branches: **Complete**
- Focused parser and targeted coverage checks recorded: **Complete**

## Evidence

### Targeted tests

```bash
uv run --python 3.12 pytest tests/unit/runtime/test_pr_monitor_runner_parts/test_pr_monitor_runner_part_017.py -q
uv run --python 3.12 pytest tests/unit/runtime/test_pr_monitor_runner_parts/test_pr_monitor_runner_part_004.py -q
uv run --python 3.12 pytest tests/unit/test_core_decomposition_maintainability.py -q
```

Observed:

- `test_pr_monitor_runner_part_017.py`: `27 passed`
- `test_pr_monitor_runner_part_004.py`: `31 passed`
- `test_core_decomposition_maintainability.py`: `9 passed`

### CI failures remediated

- `python-coverage-shards (6)` assertion in
  `test_private_awf_multiple_needs_human_uses_latest_reason` is fixed by concrete-reason
  precedence in `_parse_verdict_result`.
- `python-coverage-shards (8)` line-limit failure is fixed by moving `TestParseVerdict`
  out of the oversized file.

## Iteration 2 evidence

GitHub Actions run `28008500485` reported:

- `python-full-coverage`: combined line+branch coverage `98.997%`, below required `99.00%`.
- `ci-required`: derivative failure because `python-full-coverage` failed.

Downloaded `full-coverage-report` from the run and inspected `coverage.xml`. The PR-touched
parser helper still had uncovered branches in
`src/awf/runtime/pr_monitor_runner/helpers.py`, including the canonical AWF no-reason
fallback return and bare no-reason verdict selection. Added behavior tests in
`tests/unit/runtime/test_pr_monitor_runner_parts/test_pr_monitor_runner_part_017.py` for:

- `AWF-VERDICT: FIXED: <one-sentence summary>` returning `fix_committed` with no reason.
- `FALSE POSITIVE:` returning `false_positive` with no reason.

Focused checks:

```bash
uv run --python 3.12 pytest tests/unit/runtime/test_pr_monitor_runner_parts/test_pr_monitor_runner_part_017.py -q
uv run --python 3.12 pytest tests/unit/runtime/test_pr_monitor_runner_parts/test_pr_monitor_runner_part_017.py -q --cov=awf.runtime.pr_monitor_runner.helpers --cov-report=term-missing:skip-covered --cov-fail-under=0
```

Observed:

- Parser module: `36 passed`.
- Targeted `helpers.py` coverage diagnostic: `36 passed`; newly targeted parser lines
  are no longer listed as missing. The repository-wide `99%` coverage gate was not run
  locally; AWF/GitHub owns that broad validation after agent completion.
