# Review 4567320760 SK Prefix False Positive Plan

## Problem Statement And Scope

Review comment `issue:4567320760` reports that host setup config secret-value
scanning rejects every string starting with `sk-`, including non-secret fields
such as install channels or provider status labels (`sk-beta`, `sk-active`).

Scope is limited to `src/awf/host_setup/config.py`, focused host setup config
unit tests, and this plan/validation evidence. No branch changes, pushes,
GitHub writes, protected-file edits, or broad AWF/GitHub validation are in
scope.

## Requirements Checklist

- Add a regression proving non-secret `sk-` labels in ordinary config fields can
  be validated and persisted.
- Keep rejecting plausible raw OpenAI API key values and the existing
  unambiguous provider token prefixes.
- Preserve recursive secret scanning, secret-key rejection, and sanitized error
  diagnostics.
- Use focused local validation only; AWF/GitHub own broad validation after agent
  completion.
- Record implementation validation in
  `plans/REVIEW_4567320760_SK_PREFIX_FALSE_POSITIVE_VALIDATION.md`.

## Implementation Steps

1. Add failing focused regressions in `tests/unit/service/test_host_setup_config.py`
   for safe `sk-` labels and for plausible OpenAI key-shaped values.
2. Update `_looks_like_secret_value` so `sk-` detection requires a plausible
   OpenAI key shape instead of the broad prefix alone.
3. Replace broad dummy `sk-raw-secret-value` fixtures with structurally
   plausible fake OpenAI key strings where tests intend to exercise secret
   detection.
4. Run the new regression before implementation when practical, then run
   focused host setup config tests after implementation.
5. Record validation status and focused command evidence.

## Verification Commands And Pass Criteria

```bash
uv run --python 3.12 --extra dev pytest tests/unit/service/test_host_setup_config.py::test_host_setup_config_allows_non_secret_sk_prefixed_status_and_channel -q
uv run --python 3.12 --extra dev pytest tests/unit/service/test_host_setup_config.py -q -k "sk_prefixed or secret_payload or secret_values or uppercase_token_prefixes"
```

Pass criteria: the new safe-label regression fails before implementation and
passes after implementation; the focused host setup secret-scan tests pass.
Full AWF/GitHub validation is intentionally left to AWF after agent completion.
