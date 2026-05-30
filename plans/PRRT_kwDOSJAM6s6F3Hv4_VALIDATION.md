# PRRT_kwDOSJAM6s6F3Hv4 Validation

Plan reference: `plans/PRRT_kwDOSJAM6s6F3Hv4_PLAN.md`

## Requirement Status

- Add a regression test proving a known requested workspace id preserves real
  requested paths that would match the unknown-id artifact fallback.
  Status: Complete. Added repository coverage for known-id requested path
  filtering and retry-service coverage for the source workspace id caller.

- Allow `WorkspaceRepository.find_active_owned_path_overlaps` and the legacy
  conflict wrapper to pass a known requested workspace id into internal artifact
  filtering.
  Status: Complete. Both methods accept an optional `workspace_id` and the
  overlap method passes it to profile-derived internal artifact filtering.

- Pass the source workspace id from retry overlap detection.
  Status: Complete. `retry_workspace_row` now calls overlap lookup with
  `workspace_id=source.id`.

- Preserve existing fresh-create behavior where no workspace id exists yet.
  Status: Complete. Fresh create callers still omit `workspace_id`; the
  unknown-id fallback behavior remains covered by an existing repository test.

- Run only focused tests or checks for the changed behavior; broad AWF/GitHub
  validation remains managed after the agent phase.
  Status: Complete. Only focused tests plus targeted lint/type checks were run.

## Evidence

Files changed:

- `src/awf/db/repositories/workspace_repo.py`
- `src/awf/service/workspaces_retry.py`
- `tests/unit/db/test_workspace_repository_parts/test_workspace_repository_part_002.py`
- `tests/unit/service/test_workspace_retry.py`
- `plans/PRRT_kwDOSJAM6s6F3Hv4_PLAN.md`
- `plans/PRRT_kwDOSJAM6s6F3Hv4_VALIDATION.md`

Focused checks run:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/db/test_workspace_repository_parts/test_workspace_repository_part_002.py::TestOwnedPathOverlapLookup::test_known_requested_workspace_id_does_not_filter_other_ws_shaped_docs_path -q
```

Result: passed after implementation. The same test failed before implementation
because the repository API did not accept `workspace_id`.

```bash
uv run --python 3.12 --extra dev pytest tests/unit/service/test_workspace_retry.py::test_retry_overlap_lookup_uses_source_workspace_id_for_requested_filtering -q
```

Result: passed.

```bash
uv run --python 3.12 --extra dev pytest tests/unit/db/test_workspace_repository_parts/test_workspace_repository_part_002.py::TestOwnedPathOverlapLookup::test_custom_internal_plan_artifact_overlap_does_not_report_interworkspace_overlap tests/unit/db/test_workspace_repository_parts/test_workspace_repository_part_002.py::TestOwnedPathOverlapLookup::test_custom_profile_unknown_requested_workspace_keeps_real_ws_docs_overlap tests/unit/db/test_workspace_repository_parts/test_workspace_repository_part_002.py::TestOwnedPathOverlapLookup::test_known_requested_workspace_id_does_not_filter_other_ws_shaped_docs_path tests/unit/db/test_workspace_repository_parts/test_workspace_repository_part_002.py::TestOwnedPathOverlapLookup::test_internal_plan_artifact_filter_does_not_hide_real_overlap tests/unit/service/test_workspace_retry.py::test_retry_overlap_lookup_uses_source_workspace_id_for_requested_filtering -q
```

Result: `5 passed`.

```bash
uv run --python 3.12 --extra dev ruff check src/awf/db/repositories/workspace_repo.py src/awf/service/workspaces_retry.py tests/unit/db/test_workspace_repository_parts/test_workspace_repository_part_002.py tests/unit/service/test_workspace_retry.py
```

Result: passed.

```bash
uv run --python 3.12 --extra dev mypy src/awf/db/repositories/workspace_repo.py src/awf/service/workspaces_retry.py
```

Result: passed.

Full AWF/GitHub validation was not run locally because AWF owns broad
validation, provenance, logs, and merge gating after agent completion.
