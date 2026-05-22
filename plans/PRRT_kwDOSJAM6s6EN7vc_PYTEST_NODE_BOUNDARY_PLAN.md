# PRRT_kwDOSJAM6s6EN7vc Pytest Node Boundary Plan

## Problem Statement

Inline review thread `PRRT_kwDOSJAM6s6EN7vc` reports that the linear CI failure
evidence scanner accepts pytest node ids when punctuation or text is glued to
the path start, such as `FAILED:tests/unit/test_a.py::test_x`. That can produce
invalid retry guidance.

## Scope

- Fix only pytest node-id extraction in CI failure evidence collection.
- Preserve the bounded linear scanner behavior added for noisy CI logs.
- Preserve existing behavior for ordinary, nested, quoted, and parameterized
  pytest node ids.
- Add focused regression coverage for the reviewer-reported boundary shape.

## Requirements

- Reject pytest node candidates that are not preceded by a true token boundary.
- Do not emit `FAILED:tests/...` as a pytest node id or as part of a repro
  command.
- Keep valid `FAILED tests/... - ...` summary lines working.
- Keep focused tests and checks narrow; AWF/GitHub own broad validation after
  agent completion.

## Implementation Steps

1. Add a focused failing unit test in
   `tests/unit/runtime/test_ci_failure_evidence.py` for glued-prefix log lines.
2. Update `src/awf/runtime/ci_failure_evidence.py` so candidate path scanning
   stops at path-token boundaries and rejects unsupported prefix punctuation.
3. Run the focused regression test, then the CI failure evidence unit file if
   needed.
4. Record validation evidence in the matching validation document.

## Verification Commands

```bash
uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_ci_failure_evidence.py::test_ci_failure_evidence_rejects_glued_prefix_before_pytest_node -q
uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_ci_failure_evidence.py -q
```

Pass criteria: the new regression and focused CI failure evidence tests pass.
Full AWF/GitHub validation is intentionally left to AWF after agent completion.
