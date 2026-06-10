# Jinja2 Tojson Guard Validation

## Plan Reference

- `plans/J2_TOJSON_GUARD_PLAN.md`
- Source contract: `docs/awf-plans/ws_0d8428a5b7c44398969950db.md`

## Requirement Status

- Add `scripts/ci/check_j2_tojson.py` with `path:line` diagnostics and non-zero
  exit on unjustified raw interpolation: Complete.
- Discover tracked `docker/compose/*.j2` by default and accept explicit paths:
  Complete.
- Ignore control blocks, loop targets, and ordinary comments for violation
  detection: Complete.
- Treat final approved escaping filters, currently `tojson`, as safe: Complete.
- Support inline allowlist directives keyed by template plus normalized
  expression, with required rationale and stale/invalid detection: Complete.
- Update remaining raw interpolations in
  `docker/compose/workspace.base.yml.j2`: Complete. All remaining raw emissions
  were converted to final-expression `| tojson`; no allowlist entries were
  needed.
- Wire the checker into `.github/workflows/ci.yml` under `lint-and-type`:
  Complete.
- Add regression coverage for raw failure, escaped success, and allowlist
  behavior: Complete.

## Evidence

Red phase:

- `uv run --python 3.12 --extra dev pytest tests/unit/scripts/test_check_j2_tojson.py -q`
  failed because `scripts/ci/check_j2_tojson.py` did not exist.
- `uv run --python 3.12 --extra dev pytest tests/unit/test_ci_workflow_full_coverage.py -q -k lint_and_type`
  failed because the `lint-and-type` job did not run the checker.

Green phase:

- `uv run --python 3.12 --extra dev pytest tests/unit/scripts/test_check_j2_tojson.py -q`
  passed: 8 passed.
- `uv run --python 3.12 --extra dev pytest tests/unit/test_ci_workflow_full_coverage.py -q -k lint_and_type`
  passed: 1 passed, 16 deselected.
- `uv run --python 3.12 --extra dev python scripts/ci/check_j2_tojson.py`
  passed.
- `uv run --python 3.12 --extra dev pytest tests/unit/node/test_compose_manager.py -q`
  passed: 38 passed.
- `uv run --python 3.12 --extra dev ruff check scripts/ci/check_j2_tojson.py tests/unit/scripts/test_check_j2_tojson.py tests/unit/test_ci_workflow_full_coverage.py`
  passed.
- `uv run --python 3.12 --extra dev ruff format --check scripts/ci/check_j2_tojson.py tests/unit/scripts/test_check_j2_tojson.py tests/unit/test_ci_workflow_full_coverage.py`
  passed.
- `uv run --python 3.12 --extra dev mypy scripts/ci/check_j2_tojson.py`
  passed.

Full AWF/GitHub validation, broad coverage gates, and CI-equivalent frontend
builds were not run during the agent phase; AWF owns those after completion.

## Gaps

None.
