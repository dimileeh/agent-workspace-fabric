# Review Thread PRRT_kwDOSJAM6s6CYroz Plan

## Problem Statement And Scope

Address the unresolved PR review thread on `src/awf/common/config.py`.
The review reports that production startup currently allows
`AWF_CALLBACKS_ENABLED=true` when a strong `AWF_API_TOKEN` is configured, even
though the callback registration and list routes do not enforce that token.

Scope is limited to the production settings guardrail and focused configuration
tests that prove callback-enabled production is rejected until callback route
authentication is implemented.

## Requirements Checklist

- Prove production rejects callback-enabled startup even when a strong
  `AWF_API_TOKEN` is configured.
- Keep local and CI callback defaults usable.
- Preserve production API token and database guardrails.
- Keep diagnostics redacted and update callback diagnostic wording so it does
  not imply token configuration alone protects callback routes.
- Run the focused regression and targeted lint check.

## Implementation Steps

1. Add a focused config regression for `env=prod`, non-default database URL,
   strong API token, and `callbacks_enabled=True`.
2. Run the new regression and confirm it fails before the implementation change.
3. Change `settings_guardrails` to reject production callbacks whenever they are
   enabled, independent of API token strength.
4. Re-run the focused config tests and targeted lint.

## Verification Commands

```bash
uv run --python 3.12 --extra dev pytest tests/unit/service/test_config.py::test_production_guardrails_reject_callback_posture_with_strong_api_token_until_route_auth -q
uv run --python 3.12 --extra dev pytest tests/unit/service/test_config.py -q
uv run --python 3.12 --extra dev ruff check src/awf/common/config.py tests/unit/service/test_config.py
```

Pass criteria: the new regression fails before implementation, passes after the
guardrail change, the full config unit file remains green, and ruff reports no
issues.
