# T19 Final Integration Validation

Plan reference: `plans/T19_FINAL_INTEGRATION_PLAN.md`

## Summary

Status: satisfied.

T19 was executed locally on `development` without AWF. All backlog dependencies
are merged, first-run lanes pass, release/package integration passes, and the
final full coverage gate is green at 99.00%.

## Requirement Status

| Requirement | Status | Evidence |
| --- | --- | --- |
| Confirm T19 dependencies are merged, including T21 PR #428 | Complete | `gh pr view 428` returned `state=MERGED`, merge commit `a071c164`, merged 2026-06-06T09:15:52Z. Backlog records T01-T18, T20, and T21 complete. |
| Run integrated Python quality gates | Complete | Ruff, mypy, OpenAPI drift, unit tests, and final coverage all passed. |
| Verify first-run CLI surfaces | Complete | `awf setup --help`, `awf start --help`, `awf init --help`, and `awf mcp serve --help` all exited 0. |
| Verify source-checkout setup dry-run | Complete | Default ports were occupied by the live local AWF stack, so the successful dry-run used `AWF_API_HOST_PORT=18000 AWF_POSTGRES_HOST_PORT=15433`; JSON status was `success` with source checkout verified. |
| Verify first-run smoke lanes | Complete | `scripts/first_run_smoke.py` passed `installer-fixture`, `source-uv-run`, and `source-tool-install` lanes. |
| Verify release package integration | Complete | Wheel/sdist build, checksums, install manifest generation, release artifact drift check, and installer checksum smoke all passed under `artifacts/t19-release/`. |
| Record validation evidence | Complete | This file records the command evidence and the single coverage-gap iteration. |
| Update backlog after validation | Complete | `TODO/awf-full-installer-first-run-setup-backlog.md` marks T19 `done - validated locally` and records that the backlog is complete. |
| Commit local result only | Complete | Prepared for local commit; no push performed. |

## Command Evidence

Dependency check:

```text
gh pr view 428 --json number,state,mergedAt,mergeCommit,title,url
state=MERGED, mergeCommit=a071c1644db34ce3c8e576ee0f2704200e4da0bd
```

Static and drift gates:

```text
uv run --python 3.12 --extra dev ruff check src/awf tests
All checks passed!

uv run --python 3.12 --extra dev mypy src/awf
Success: no issues found in 354 source files

uv run --python 3.12 --extra dev python scripts/generate_openapi.py --check
OK: openapi.json matches the current app spec.
```

Standalone unit gate:

```text
uv run --python 3.12 --extra dev pytest tests/unit -q
11552 passed, 1 skipped in 3902.08s (1:05:02)
```

Coverage iteration:

```text
uv run --python 3.12 --extra dev pytest -n 20 --timeout=300 --cov=awf --cov-report=term-missing --cov-fail-under=99
11655 passed, 1 skipped in 548.02s (0:09:08)
Coverage failure: total of 98.99 is less than fail-under=99.00
```

Root cause: the merged T19 dependency surface left the aggregate just below the
99% threshold. The uncovered path selected for the required regression was
`_check_worker_heartbeat`'s stale heartbeat branch in
`src/awf/api/routes/health.py`, which is real readiness behavior.

Fix evidence:

```text
uv run --python 3.12 --extra dev pytest tests/unit/api/test_health_parts/test_health_part_001.py::test_worker_heartbeat_check_stale_result_includes_age_and_threshold -q
1 passed in 0.17s

uv run --python 3.12 --extra dev ruff check tests/unit/api/test_health_parts/test_health_part_001.py
All checks passed!
```

Final coverage gate:

```text
uv run --python 3.12 --extra dev pytest -n 20 --timeout=300 --cov=awf --cov-report=term-missing --cov-fail-under=99
11656 passed, 1 skipped in 540.14s (0:09:00)
Required test coverage of 99% reached. Total coverage: 99.00%
```

CLI and MCP surfaces:

```text
uv run --python 3.12 --extra dev awf setup --help
uv run --python 3.12 --extra dev awf start --help
uv run --python 3.12 --extra dev awf init --help
uv run --python 3.12 --extra dev awf mcp serve --help
```

All four help commands exited 0.

MCP setup tool registration:

```text
MCP setup tools registered: awf_get_client_integration_instructions, awf_get_setup_status, awf_initialize_project_profile, awf_start_local_service
```

Setup dry-run:

```text
AWF_API_HOST_PORT=18000 AWF_POSTGRES_HOST_PORT=15433 uv run --python 3.12 --extra dev awf setup --dry-run --source-checkout "$PWD" --format json
status=success
summary=AWF setup host readiness checks passed; this machine can run AWF Core.
source_checkout.root=/home/dmitri-lihhatsov/Projects/aira-agent-workspace-fabric
```

First-run smoke:

```text
uv run --python 3.12 --extra dev python scripts/first_run_smoke.py --lane installer-fixture --lane source-uv-run --lane source-tool-install --checkout-root "$PWD"
PASSED installer-fixture
PASSED source-uv-run awf --help/setup --help/start --help/setup --dry-run
PASSED source-tool-install uv tool install plus awf --help/setup --help/start --help/setup --dry-run
```

Release/package integration:

```text
uv run --python 3.12 --with build python -m build --outdir artifacts/t19-release/dist
Successfully built agent_workspace_fabric-0.1.0.tar.gz and agent_workspace_fabric-0.1.0-py3-none-any.whl

sha256sum artifacts/t19-release/dist/* | tee artifacts/t19-release/release/python-distribution-sha256.txt
1cdbb132a8b307ce014cf76658f17fd48e9d6cf0036ef6a7b20f30d78df8c7c3  artifacts/t19-release/dist/agent_workspace_fabric-0.1.0-py3-none-any.whl
a83722d7187a92c53945873667991a7687337f57062c74d87bf0b7ff782fdff6  artifacts/t19-release/dist/agent_workspace_fabric-0.1.0.tar.gz

uv run --python 3.12 python scripts/generate_install_manifest.py ...
OK: wrote artifacts/t19-release/release/awf-install-manifest.json

uv run --python 3.12 python scripts/check_release_artifacts.py ...
OK: artifacts/t19-release/release/awf-install-manifest.json matches the built distributions

uv run --no-project --python 3.12 python scripts/release_smoke.py ... --run
Checksum verified for agent_workspace_fabric-0.1.0-py3-none-any.whl.
Dry run complete; no changes were made.
```

## Gaps

None.

## Notes

- The first setup dry-run with default ports correctly returned
  `SETUP_READINESS_FAILED` because the live local AWF stack was already using
  API port 8000 and Postgres port 5433. The rerun with alternate free ports
  proves source-checkout readiness without stopping the running stack.
- The standalone unit suite was run before adding the coverage top-up test. The
  added test passed directly, and the final all-tests coverage run included it
  and passed, so the final test state is covered by the green coverage gate.
