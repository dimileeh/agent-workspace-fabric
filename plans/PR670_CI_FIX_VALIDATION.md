# PR #670 CI failure repair validation

## Plan reference

- `plans/PR670_CI_FIX_PLAN.md`

## Requirement status

- Branch discipline / no broad CI: **Complete**
- Verdict parsing fix: **Complete**
- Line-limit split: **Complete**
- Targeted regression checks: **Complete**
- Validation report recorded: **Complete**

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
