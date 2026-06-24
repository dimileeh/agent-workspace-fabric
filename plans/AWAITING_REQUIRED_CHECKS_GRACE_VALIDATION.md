# Validation: Expose `awaiting_required_checks_grace_seconds` as a ProfileMonitor knob (#662)

Validates the implementation in `plans/AWAITING_REQUIRED_CHECKS_GRACE_PLAN.md`
(kept at `docs/awf-plans/ws_4509b5fc34874909b66d9bb1.md` in the workspace) against
the saved plan. Scope is strictly additive wiring mirroring the existing monitor
knobs end-to-end — no `decide()` gate logic, no grace behavior, no other monitor
work changes.

## Plan → implementation trace

| Plan step | File(s) | Status |
| --- | --- | --- |
| 1. Add `awaiting_required_checks_grace_seconds: float = Field(default=600.0, le=86400)` to `ProfileMonitor`, no lower bound (permits the documented `<= 0` disable), docstring referencing #655/#662. | `src/awf/profiles/models.py` | ✅ done — default `600.0` preserves today's behavior, `le=86400` only (no `ge`), docstring added. |
| 2. Thread it through `worker.py` `monitor_kwargs` like `require_ci` / `non_check_reviewer_settle_seconds`. | `src/awf/service/worker.py` | ✅ done — `awaiting_required_checks_grace_seconds` added to `monitor_kwargs`. |
| 3. Add `awaiting_required_checks_grace_seconds: float = 600.0` to both `build_release_pr_monitor` and `build_feature_pr_monitor`; pass into each `MonitorConfig(...)`. | `src/awf/runtime/release_pr_monitor.py` | ✅ done — both builders, default `600.0` safety net, threaded into both `MonitorConfig` constructors. |
| 4. Regenerate `openapi.json`; keep `--check` green. | `openapi.json` | ✅ done — `ProfileMonitor` schema gained the `awaiting_required_checks_grace_seconds` property; `python scripts/generate_openapi.py --check` passes. |
| 4 (docs). Add a `docs/CONCEPTS.md` subsection under the monitor area documenting the knob (default 600s, `<= 0` disables). | `docs/CONCEPTS.md` | ✅ done — "Awaiting-Required-Checks Grace" subsection added after "Non-Actionable Bot Comments", consistent with the "Initial Review Grace" style. |
| 5. Wiring tests: profile that sets → flows to `MonitorConfig`; profile that omits → keeps `600.0`; `<= 0` disables (reaches `MonitorConfig` as `<= 0`). | `tests/unit/profiles/test_profile_monitor.py`, `tests/unit/runtime/test_release_pr_monitor.py`, `tests/unit/service/test_worker.py` | ✅ done — see test inventory below. |

## Test inventory (new/extended)

`tests/unit/profiles/test_profile_monitor.py`:
- `test_profile_monitor_awaiting_required_checks_grace_defaults_600` — default 600.0.
- `test_profile_schema_accepts_awaiting_required_checks_grace_seconds` — set 120 parses.
- `test_profile_schema_accepts_awaiting_required_checks_grace_seconds_zero_or_negative` — `0` and `-1` parse (documented disable).
- `test_profile_schema_rejects_awaiting_required_checks_grace_seconds_above_86400` — `86401` raises `ValidationError` (bounds parity).

`tests/unit/runtime/test_release_pr_monitor.py`:
- `test_factories_plumb_configured_knobs` — extended to pass `awaiting_required_checks_grace_seconds=250` and assert `runner._config.awaiting_required_checks_grace_seconds == 250`.
- `test_factories_awaiting_required_checks_grace_defaults_600` (parametrized over both builders) — omitting the kwarg yields `600.0`.
- `test_factories_awaiting_required_checks_grace_zero_disables` (parametrized) — `0` reaches `MonitorConfig`.

`tests/unit/service/test_worker.py`:
- Default profile case — asserts `feature_monitor_kwargs["awaiting_required_checks_grace_seconds"] == 600`.
- Custom `ProfileMonitor(...)` — adds `awaiting_required_checks_grace_seconds=250`; asserts it flows to both feature and release monitor kwargs.
- Focused disable case — `ProfileMonitor(awaiting_required_checks_grace_seconds=0)` flows `0` to feature monitor kwargs.

## Focused validation commands (run inside the agent phase)

Per the AWF workspace contract (rule 4), the full AWF/GitHub validation suite
(full `pytest --cov` gate, broad lint/type over `src/awf` and `tests`, full
frontend build, CI-equivalent commands) is managed by AWF and GitHub CI after
agent completion — **not** run here. Focused checks below cover the touched
behavior.

| Command | Result |
| --- | --- |
| `uv run --python 3.12 --extra dev pytest tests/unit/profiles/test_profile_monitor.py tests/unit/runtime/test_release_pr_monitor.py tests/unit/service/test_worker.py -q` | `38 passed` ✅ |
| `uv run --python 3.12 --extra dev ruff check src/awf/profiles/models.py src/awf/service/worker.py src/awf/runtime/release_pr_monitor.py tests/unit/profiles/test_profile_monitor.py tests/unit/runtime/test_release_pr_monitor.py tests/unit/service/test_worker.py` | `All checks passed!` ✅ |
| `uv run --python 3.12 --extra dev mypy src/awf/profiles/models.py src/awf/service/worker.py src/awf/runtime/release_pr_monitor.py` | `Success: no issues found in 3 source files` ✅ |
| `uv run --python 3.12 --extra dev python scripts/generate_openapi.py --check` | `OK: openapi.json matches the current app spec.` ✅ |

## Non-goals honored

- `decide()` gate logic, the grace behavior in
  `pr_monitor_runner/helpers.py`, `notify_human_loop.py`, `merge_loop.py`: untouched.
- No per-workspace DB override column added (profile-only configurability, as scoped).
- `pr_monitor_adoption.py`, MCP/CLI/REST adoption schemas, `workspaces_create.py`,
  `workspaces_retry.py`, `provider_recovery.py`, `workspace_repo.py` untouched —
  the per-workspace `initial_review_grace_period_seconds` override surface is out
  of scope.
- No unrelated refactor/rename/restructure.

## Diff summary

```
 docs/CONCEPTS.md                              | 23 ++++++++++++
 openapi.json                                  |  6 ++++
 src/awf/profiles/models.py                    | 15 ++++++++
 src/awf/runtime/release_pr_monitor.py         |  4 +++
 src/awf/service/worker.py                     |  3 ++
 tests/unit/profiles/test_profile_monitor.py   | 52 +++++++++++++++++++++++++--
 tests/unit/runtime/test_release_pr_monitor.py | 35 ++++++++++++++++++
 tests/unit/service/test_worker.py             | 27 ++++++++++++++
 8 files changed, 163 insertions(+), 2 deletions(-)
```

Additive, scoped, mirrors the existing monitor knobs end-to-end. No gaps found
against the saved plan.
