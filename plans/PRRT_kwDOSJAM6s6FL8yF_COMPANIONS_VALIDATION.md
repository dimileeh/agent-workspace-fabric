# PRRT_kwDOSJAM6s6FL8yF Companions Validation

Plan reference: `plans/PRRT_kwDOSJAM6s6FL8yF_COMPANIONS_PLAN.md`

## Requirement Status

- Persist `companions: []` in workspace create task-policy snapshots for requests
  with no companions: Complete.
- Preserve the existing serialized companion representation for non-empty
  companion lists: Complete.
- Keep the change scoped to workspace create policy snapshot behavior and its
  focused unit tests: Complete.
- Use targeted validation only; full AWF/GitHub validation is managed after agent
  completion: Complete.

## Evidence

Files changed:

- `src/awf/service/workspaces_create.py`
- `tests/unit/service/test_workspaces_observability_parts/test_workspaces_observability_part_001.py`
- `plans/PRRT_kwDOSJAM6s6FL8yF_COMPANIONS_PLAN.md`
- `plans/PRRT_kwDOSJAM6s6FL8yF_COMPANIONS_VALIDATION.md`

Focused failing evidence before implementation:

- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_workspaces_observability_parts/test_workspaces_observability_part_001.py -q -k "task_policy_and_profile_tier_helpers or companion"`
  failed because `policy` omitted the `companions` key for a request with no
  companions.

Focused passing evidence after implementation:

- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_workspaces_observability_parts/test_workspaces_observability_part_001.py -q -k "task_policy_and_profile_tier_helpers or companion"`
  passed: 2 passed, 56 deselected.
- `uv run --python 3.12 --extra dev pytest tests/unit/api/test_workspaces_direct.py -q -k "companions"`
  passed: 1 passed, 18 deselected.
- `uv run --python 3.12 --extra dev ruff check src/awf/service/workspaces_create.py tests/unit/service/test_workspaces_observability_parts/test_workspaces_observability_part_001.py`
  passed.
- `git diff --check` passed.

No gaps remain in the saved plan. Full repository validation, coverage, and
CI-equivalent checks are left to AWF/GitHub after this agent phase.
