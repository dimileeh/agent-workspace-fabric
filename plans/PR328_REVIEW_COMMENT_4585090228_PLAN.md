# PR #328 Review Comment 4585090228 Plan

## Problem Statement

Greptile's review-level comment on PR #328 calls out four design/documentation
concerns around host-port conflict handling and terminal-runtime release
revocation. The checkout already appears to address the logging, pre-launch
profile invariant comment, and repository scan documentation concerns. The
remaining actionable mismatch is that revoke escalation documentation and the
operator-facing escalation message say "consecutive" even though the
implementation counts lifetime-total revoke events.

## Scope

- Keep the fix limited to the cited review comment cluster.
- Do not change host-port admission behavior or retry semantics.
- Do not run broad AWF/GitHub-owned validation.
- Commit the local fix on the current AWF-managed branch.

## Requirements Checklist

- [ ] Update a focused regression assertion for revoke escalation wording so the
      lifetime-total semantics are preserved.
- [ ] Update provisioner revoke escalation documentation/message to describe
      lifetime-total counting.
- [ ] Confirm the cited best-effort cleanup wrapper logs exceptions.
- [ ] Confirm the pre-launch profile write invariant is documented.
- [ ] Confirm host-port conflict scan scalability is documented in the
      repository docstring and defer production EXPLAIN analysis to rollout.
- [ ] Run only targeted tests/checks for the touched behavior.

## Implementation Steps

1. Add or update a narrow unit assertion around the REVOKE_CAP_REACHED payload.
2. Run the targeted test to confirm the assertion fails before the code change.
3. Update `src/awf/node/provisioner.py` to use lifetime-total terminology.
4. Run the targeted test again.
5. Create the validation document with requirement-by-requirement evidence.
6. Stage only changed files and commit with the requested review-comment
   message shape.

## Verification Commands

- `uv run --python 3.12 --extra dev pytest tests/unit/node/test_provisioner_parts/test_provisioner_part_002.py::TestOperatorControlRaces::test_orphan_stop_failure_over_cap_records_revoke_cap_escalation -q`

Pass criteria: the targeted test fails before implementation, passes after the
terminology update, and no broad validation suite is run inside the agent phase.
