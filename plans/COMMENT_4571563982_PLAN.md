# Comment 4571563982 Plan

## Problem Statement and Scope

Address PR review-level feedback from comment `issue:4571563982` about the
first-run redaction/rendering implementation. The feedback is not reporting a
runtime correctness failure; it asks for clearer contracts and better
co-location of provider-ref redaction patterns.

Scope is limited to first-run/log redaction helpers, their focused unit tests,
and this plan/validation documentation. Branch management, pushing, full AWF
validation, and GitHub PR bookkeeping remain owned by AWF.

## Requirements Checklist

- [ ] Preserve the existing distinct redaction markers for audit/first-run
      output (`[redacted]`) and runtime logs (`<redacted>`) while documenting that
      they are separate output contracts.
- [ ] Move provider-ref regex pattern definitions out of
      `awf.host_setup.rendering` and into lightweight shared common redaction
      pattern code, without importing Pydantic rendering models from common
      callers.
- [ ] Keep first-run provider-ref redaction behavior unchanged for URI values,
      provider/credential-ref keys, key suffix handling, and redacted-key
      collision behavior.
- [ ] Document that helper-built blocked/failed payloads intentionally use the
      command status as the single issue severity.
- [ ] Run only focused local checks that cover the changed behavior; leave
      broad AWF/GitHub validation to the post-agent workflow.

## Implementation Steps

1. Add shared provider-ref pattern constants and compile helpers to
   `src/awf/common/token_patterns.py`.
2. Update `src/awf/host_setup/rendering.py` to consume the shared provider-ref
   compile helpers instead of owning raw regex strings.
3. Add concise comments/docstrings for the marker divergence and
   `severity=status` contract.
4. Add or update focused tests only if existing tests do not already lock the
   behavior being preserved.
5. Run targeted tests for common redaction and host setup rendering.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/common/test_token_patterns.py tests/unit/runtime/test_log_redaction.py tests/unit/service/test_host_setup_rendering.py -q`
  - Passes, proving the shared pattern source, runtime-log redaction, and
    first-run provider-ref rendering still behave as expected.
- `uv run --python 3.12 --extra dev ruff check src/awf/common/token_patterns.py src/awf/common/redaction.py src/awf/host_setup/rendering.py tests/unit/common/test_token_patterns.py`
  - Passes, proving touched Python files satisfy focused lint rules.

Full repository validation, coverage gates, and CI-equivalent frontend/build
checks are intentionally not run in this agent phase; AWF/GitHub owns those
broader gates after completion.
