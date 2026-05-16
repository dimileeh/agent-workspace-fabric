# Review Thread PRRT_kwDOSJAM6s6CYszr Plan

## Problem Statement And Scope

Address the unresolved PR review thread on `src/awf/common/config.py`.
The review reports that production database guardrail parsing catches
`ValueError` from `SplitResult.port`, masking malformed database URL details
that should bubble to the calling phase handler.

Scope is limited to the default local database URL/credential detector and
focused configuration tests proving malformed production database URLs are not
converted into generic production guardrail diagnostics.

## Requirements Checklist

- Prove malformed production database URL ports bubble as `ValueError`.
- Preserve production rejection of bundled local database credentials.
- Keep local and CI defaults usable.
- Run the focused regression and targeted lint check.

## Implementation Steps

1. Add a focused config regression for `env=prod` with default local database
   credentials and a malformed non-integer port.
2. Run the new regression and confirm it fails before the implementation
   change.
3. Remove local `ValueError` handling around `urlsplit(...).port` and keep URL
   component access explicit so parse failures bubble.
4. Re-run the focused config tests and targeted lint.

## Verification Commands

```bash
uv run --python 3.12 --extra dev pytest tests/unit/service/test_config.py::test_production_guardrails_let_malformed_database_url_port_bubble -q
uv run --python 3.12 --extra dev pytest tests/unit/service/test_config.py -q
uv run --python 3.12 --extra dev ruff check src/awf/common/config.py tests/unit/service/test_config.py
```

Pass criteria: the new regression fails before implementation, passes after the
guardrail change, the full config unit file remains green, and ruff reports no
issues.
