# Fix PR 328 CI Plan

## Problem Statement and Scope

PR #328 fails the focused Python CI repro in three areas:

- `ControlWorker._release_terminal_runtime_resources` adds a secondary
  planning-scope retry resume-scan failure to the grouped terminal runtime
  release candidate failures, so the edge test sees three exceptions instead
  of the two candidate failures under test.
- `tests/unit/service/test_worker.py` still monkeypatches the old
  `awf.service.worker.GitHubClient` implementation detail even though worker
  monitor wiring now builds forge clients through `make_forge_client`.
- Four first-party test files exceed the repository's 1,500 line
  maintainability limit after recent review-fix additions.

The scope is limited to fixing those failing behaviors without weakening tests,
quality gates, workflows, or AWF/GitHub-owned validation.

## Requirements Checklist

- [ ] Preserve grouped terminal runtime release failures.
- [ ] Keep the planning-scope retry safety scan behavior, but do not let a
      secondary safety-scan failure pollute grouped candidate release failures.
- [ ] Update the service worker unit test to stub current forge-client wiring.
- [ ] Keep every first-party source/test file at or under 1,500 lines.
- [ ] Run only focused repro/validation commands locally.
- [ ] Commit the local fix on the current AWF branch without pushing.

## Implementation Steps

1. Inspect the terminal runtime release control flow and update error grouping
   so candidate release failures remain the raised primary failures while the
   existing retry-resume safety scan can still run.
2. Update the service worker test monkeypatches to match `make_forge_client`
   based monitor wiring.
3. Split oversized test modules using existing test-part patterns, preserving
   the same test behavior.
4. Re-run the focused CI repro command and any narrow import/type checks needed
   for moved tests.
5. Record validation evidence in `plans/fix_pr328_ci_VALIDATION.md`.
6. Commit the scoped fix locally with a conventional commit message.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker_coverage_edges_parts/test_worker_coverage_edges_part_001.py::test_terminal_runtime_release_groups_multiple_candidate_failures tests/unit/service/test_worker.py::test_build_worker_runtime_defaults_unset_service_node_id_to_local tests/unit/node/test_provisioner_parts/test_provisioner_part_002.py::TestOperatorControlRaces::test_orphan_stop_timeout_records_false_in_payload tests/unit/test_core_decomposition_maintainability.py::test_first_party_code_files_stay_under_line_limit -q`
  passes.
- Any new split test file imports cleanly through the targeted pytest command.
- Full AWF/GitHub validation remains owned by AWF after agent completion.
