# CI Workspaces Idempotency Replay Validation

Plan reference: `plans/ci_workspaces_idempotency_replay_PLAN.md`

## Requirement Status

- Reproduce the failure with the narrowest practical local command: Complete.
  - `AWF_HOST_HOME=<empty-dir> ... test_v2_warm_cache_replay_uses_durable_auto_profile_match` failed before the fix with `409 != 202`.
  - A serial `tests/unit/api/test_workspaces.py` run under load exposed the adjacent fixed-window rate-limit flake with `202 != 429`.
- Identify the root cause in production code or test isolation: Complete.
  - The provider-readiness replay test relied on ambient host Codex auth despite a class fixture clearing provider auth env.
  - Workspace rate-limit tests used the real fixed-window limiter clock, so slow creates could cross the 60-second window and admit a fresh request.
- Add or update regression coverage for the behavior involved: Complete.
  - `tests/unit/api/test_workspaces.py` now installs a stable request-admission limiter clock for workspace API test apps/direct request helpers.
  - The provider-readiness replay test explicitly seeds Codex auth.
  - The reported idempotency replay tests use invocation-unique idempotency keys to avoid durable replay collisions with leaked or reused state.
- Keep the change scoped to workspace API idempotency/rate-limit behavior: Complete.
  - Only `tests/unit/api/test_workspaces.py` and plan/validation docs changed.
- Run focused verification for the failing nodes and enough surrounding tests to prove the leak is fixed: Complete.
  - See command evidence below.
- Commit locally with a conventional commit message that names the failed check and root cause: Complete for this fix cycle.

## Evidence

Commands passed:

```bash
tmpdir=$(mktemp -d); AWF_HOST_HOME="$tmpdir" uv run --python 3.12 --extra dev pytest tests/unit/api/test_workspaces.py::TestWorkspaceCreateProviderReadinessPreflight::test_v2_warm_cache_replay_uses_durable_auto_profile_match -q; rc=$?; rm -rf "$tmpdir"; exit $rc
```

```bash
uv run --python 3.12 --extra dev pytest tests/unit/api/test_workspaces.py::TestCreateWorkspaceV2DiskPressure::test_v2_create_rate_limit_rejects_before_disk_admission tests/unit/api/test_workspaces.py::TestCreateWorkspaceV2DiskPressure::test_v2_idempotency_replay_bypasses_limit_but_fresh_keys_are_bounded tests/unit/api/test_workspaces.py::TestWorkspaceCreateProviderReadinessPreflight::test_v2_warm_cache_replay_uses_durable_auto_profile_match -q
```

```bash
uv run --python 3.12 --extra dev ruff check tests/unit/api/test_workspaces.py
```

```bash
uv run --python 3.12 --extra dev pytest tests/unit/api/test_workspaces.py -q
```

```bash
uv run --python 3.12 --extra dev pytest -n 8 --dist=loadscope --timeout=300 tests/unit/api/test_workspaces.py -q
```

Diagnostic note: a file-only `--cov=awf` run passed tests but returned non-zero because the repository-wide `--cov-fail-under=99` threshold is not meaningful for a single test file.
