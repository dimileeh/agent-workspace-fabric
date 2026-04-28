# PR #93 P1 Monitor Completion Metrics Plan

## Intended Files and Modules to Touch

1. `tests/unit/service/test_metrics.py`
   - Add a focused regression test for `awf.service.metrics._count_monitor_completions`.
   - Reuse the existing async DB fixtures and SQLAlchemy `before_cursor_execute` event pattern already present in this file.
   - Seed workspace rows that exercise all three monitor counters and edge cases:
     - recent PR workspace counts toward `monitor_completed_total`;
     - recent completed PR workspace counts toward both `monitor_completed_total` and `completed_after_monitor_count`;
     - completed workspace without `pr_url` is excluded from PR-monitor completion counters;
     - old PR workspace outside the `updated_at` window is excluded from recent PR counters;
     - old `monitoring_pr` workspace older than `2 * SLA` counts toward `monitor_stuck_count`, regardless of PR completion status.
   - During the direct `_count_monitor_completions` call, patch the session's `scalar` method to fail if called and capture SQL statements to assert the helper performs exactly one SELECT round trip.

2. `src/awf/service/metrics.py`
   - Replace `_count_monitor_completions`' current `asyncio.gather(session.scalar(...), ...)` implementation with one aggregate `select`.
   - Use `func.sum(case(...))` or `func.count(case(...))` labels for:
     - `monitor_completed_total`: `Workspace.updated_at >= window_start` and `Workspace.pr_url.is_not(None)`;
     - `completed_after_monitor`: completed status plus the same recent PR predicate;
     - `monitor_stuck`: `Workspace.status == monitoring_pr` and `Workspace.created_at < now - timedelta(seconds=2 * sla_seconds)`.
   - Execute that single statement with `await session.execute(stmt)`, read one row, and coerce `None` aggregate values to `0`.

3. `docs/awf-plans/ws_4eab2b4971de4cfd99c75b8f.md`
   - Planning artifact only. This file is the only file to change during the current planning phase.

## Tests to Write First

1. Add `test_count_monitor_completions_uses_single_aggregate_execute` in `tests/unit/service/test_metrics.py`.
   - Import `_count_monitor_completions` inside the test, matching the local import style in the file.
   - Seed the rows listed above with a fixed `now = datetime(2026, 4, 28, 12, 0, tzinfo=UTC)` and `sla_seconds = 3600`.
   - Attach `event.listen(engine.sync_engine, "before_cursor_execute", record_sql)` immediately before calling the helper so setup queries are not counted.
   - Patch `session.scalar` with an async function that raises `AssertionError`, which makes the current gather/scalar implementation fail before the fix.
   - Assert the returned tuple preserves semantics, for example `(3, 1, 1)` or the final seeded equivalent.
   - Assert exactly one captured statement starts with `select`, proving a single round trip.

2. Keep the existing SLO summary tests, especially:
   - `test_monitor_metrics_counts_completed_and_stuck`;
   - `test_monitoring_pr_not_counted_in_stuck_detailed`;
   - `test_slo_summary_returns_zero_counts_for_empty_db`.

3. Run the focused test before implementation and confirm it fails against the current `session.scalar`/`asyncio.gather` code.

## Implementation Approach

1. Compute `cutoff = now - timedelta(seconds=2 * sla_seconds)` exactly as the current helper does.
2. Build boolean predicates for recent PR workspaces, completed recent PR workspaces, and stuck monitor workspaces.
3. Create one aggregate statement:

```python
stmt = select(
    func.sum(case((recent_pr_predicate, 1), else_=0)).label("monitor_completed_total"),
    func.sum(case((completed_recent_pr_predicate, 1), else_=0)).label("completed_after_monitor"),
    func.sum(case((monitor_stuck_predicate, 1), else_=0)).label("monitor_stuck"),
).select_from(Workspace)
```

4. Replace the `asyncio.gather` block with:

```python
row = (await session.execute(stmt)).one()
return (
    int(row.monitor_completed_total or 0),
    int(row.completed_after_monitor or 0),
    int(row.monitor_stuck or 0),
)
```

5. Leave the `asyncio` import in place because `metrics.py` still uses `asyncio.to_thread` elsewhere.
6. After validation, commit locally with a conventional message such as `fix(metrics): aggregate monitor completion counts`. Do not push manually.

## Validation Commands

TDD failure check after adding the test:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/service/test_metrics.py -q -k "count_monitor_completions"
```

Focused green check after implementation:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/service/test_metrics.py -q -k "monitor"
```

Required broader validation for the touched Python/control-plane surface:

```bash
uv run --python 3.12 --extra dev ruff check src/awf tests
uv run --python 3.12 --extra dev mypy src/awf
uv run --python 3.12 --extra dev pytest tests/unit -q
```

Coverage gate if the change is treated as core behavior by the integrator:

```bash
uv run --python 3.12 --extra dev pytest --cov=awf --cov-report=term-missing
```

## Risks

- The aggregate query must not accidentally share the recent PR predicate with `monitor_stuck`; the stuck metric is based only on `monitoring_pr` status and age.
- `SUM(CASE ...)` returns `NULL` on an empty table in some databases, so explicit `or 0` coercion is required to preserve zero-count semantics.
- SQL text differs by dialect, so the regression test should count statements and forbid `session.scalar` rather than asserting brittle full SQL strings.
- The test targets a private helper by design. This is acceptable here because the finding is specifically about that helper's unsafe session access pattern.

## Assumptions

- `pr_url is not None` remains the marker for a workspace that reached PR-monitor work.
- "Recent" for `monitor_completed_total` and `completed_after_monitor_count` continues to mean `Workspace.updated_at >= window_start`.
- `monitor_stuck_count` continues to use `Workspace.created_at < now - 2 * SLA`, matching the current code and task wording.
- No API schema, database schema, migration, console, or route behavior changes are needed.

## Explicit Non-Goals

- Do not alter `SloMetricsSummary` field names or response serialization.
- Do not change creation, cleanup, recovery, stuck-running, or reason-code metrics.
- Do not introduce retries, new sessions, or additional DB round trips to hide async-session concurrency problems.
- Do not modify migrations, lockfiles, frontend code, or unrelated docs.
- Do not switch branches, push, rebase, force-push, or commit during this planning phase.
