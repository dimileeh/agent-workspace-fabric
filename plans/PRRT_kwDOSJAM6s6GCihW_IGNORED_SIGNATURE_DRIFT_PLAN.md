# PRRT_kwDOSJAM6s6GCihW Ignored Signature Drift Plan

## Problem Statement And Scope

PR review thread `PRRT_kwDOSJAM6s6GCihW` reports that executor validation records
the setup ignored snapshot paths but does not compare baseline content
signatures. A validation fix pass can mutate an existing ignored file without
adding or removing ignored paths, and the next validation attempt would not
detect the mutation.

Scope is limited to executor validation ignored-snapshot drift detection and its
focused unit coverage.

## Requirements Checklist

- Detect content-signature drift for ignored snapshot paths captured at setup.
- Preserve existing added/removed ignored-root and ignored-path drift behavior.
- Add a regression test where ignored roots and paths stay constant but a
  baseline ignored file signature changes.
- Run only targeted local checks; full AWF/GitHub validation remains managed by
  AWF after agent completion.

## Implementation Steps

1. Extend the executor ignored-snapshot drift helper to accept setup and current
   ignored snapshot signatures.
2. Store setup ignored snapshot signatures alongside setup ignored roots and
   paths.
3. Pass current signatures into the drift helper on later validation attempts.
4. Add a focused regression test in the existing executor coverage edge tests.
5. Run the targeted test selection that covers the changed behavior.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor_coverage_edges_parts/test_executor_coverage_edges_part_003.py -q -k "ignored"`
  - Passes and includes the new same-path signature-drift regression.
