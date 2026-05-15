# Review Thread PRRT_kwDOSJAM6s6CZh_c Plan

## Problem Statement And Scope

Address the unresolved PR review thread on `src/awf/common/config.py`.
The review reports that production API token guardrails reject separated
repetitions of weak placeholder values, but miss separatorless repetitions such
as `secretsecretsecretsecret`.

Scope is limited to weak API token detection and focused configuration tests.

## Requirements Checklist

- Reject separatorless repeated weak API token placeholders in production.
- Preserve existing rejection for missing, short, exact, and separated weak
  token values.
- Keep strong production API tokens accepted when other production settings are
  valid.
- Run the focused regression and targeted config validation.

## Implementation Steps

1. Add a focused regression covering `secretsecretsecretsecret` in the existing
   weak production API token test parameterization.
2. Run the focused regression and confirm it fails before the implementation
   change.
3. Update repeated weak-token detection to include adjacent repeated weak
   values in addition to separator-delimited values.
4. Re-run the focused regression, full config unit tests, and targeted ruff.

## Verification Commands

```bash
uv run --python 3.12 --extra dev pytest tests/unit/service/test_config.py::test_production_guardrails_reject_missing_or_weak_api_token -q
uv run --python 3.12 --extra dev pytest tests/unit/service/test_config.py -q
uv run --python 3.12 --extra dev ruff check src/awf/common/config.py tests/unit/service/test_config.py
```

Pass criteria: the separatorless repeated placeholder regression fails before
implementation, passes after the guardrail change, the full config unit file
remains green, and ruff reports no issues.
