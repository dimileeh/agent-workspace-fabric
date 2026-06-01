# PR #328 Review Comment 4585090228 Validation

Plan reference: `plans/PR328_REVIEW_COMMENT_4585090228_PLAN.md`

## Requirement Status

- Complete: Updated a focused regression assertion for revoke escalation wording
  in `tests/unit/node/test_provisioner_parts/test_provisioner_part_002.py`.
- Complete: Updated provisioner revoke escalation documentation and the
  REVOKE_CAP_REACHED operator message in `src/awf/node/provisioner.py` to say
  the counter is lifetime-total, not consecutive.
- Complete: Confirmed `_launch_lost_to_terminal_cleanup_best_effort` logs
  caught exceptions via
  `provisioner.launch_lost_to_terminal_cleanup_check_failed`.
- Complete: Confirmed the pre-launch profile write block documents why
  `resolved_profile` is only written under the row-locked provisioning guard.
- Complete: Confirmed `find_host_port_conflicts` documents the full scan and
  future index/denormalization options. Production `EXPLAIN ANALYZE` remains a
  rollout/operations follow-up because no production data is available inside
  this AWF workspace.
- Complete: Ran only focused local checks; full AWF/GitHub validation is
  managed after agent completion.

## Evidence

- Pre-implementation targeted test result:
  `uv run --python 3.12 --extra dev pytest tests/unit/node/test_provisioner_parts/test_provisioner_part_002.py::TestOperatorControlRaces::test_orphan_stop_failure_over_cap_records_revoke_cap_escalation -q`
  failed as expected because the payload message still said
  `5 consecutive revoke events`.
- Post-implementation targeted test result:
  `uv run --python 3.12 --extra dev pytest tests/unit/node/test_provisioner_parts/test_provisioner_part_002.py::TestOperatorControlRaces::test_orphan_stop_failure_over_cap_records_revoke_cap_escalation -q`
  passed (`1 passed`).
- Focused lint result:
  `uv run --python 3.12 --extra dev ruff check src/awf/node/provisioner.py tests/unit/node/test_provisioner_parts/test_provisioner_part_002.py`
  passed.
- Text confirmation:
  `rg -n "consecutive revoke|lifetime-total revoke|launch_lost_to_terminal_cleanup_check_failed|resolved_profile update is intentionally|no WHERE clause that prunes by port" src/awf/node/provisioner.py src/awf/db/repositories/workspace_repo_host_ports.py tests/unit/node/test_provisioner_parts/test_provisioner_part_002.py`
  found the new lifetime-total wording and the already-present logging and
  documentation cited by the review comment.

## Remaining Gaps

None for this review-comment fix. Broad validation, CI-equivalent checks, and
production-scale query analysis were not run inside the agent phase per the AWF
workspace contract.
