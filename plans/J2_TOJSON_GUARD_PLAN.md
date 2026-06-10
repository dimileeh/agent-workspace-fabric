# Jinja2 Tojson Guard Plan

## Problem Statement And Scope

Implement the saved AWF plan in
`docs/awf-plans/ws_0d8428a5b7c44398969950db.md`: add a CI lint guard for
structured Docker Compose Jinja2 templates so value-emitting `{{ ... }}`
interpolations in `docker/compose/*.j2` are escaped with `| tojson` or covered
by a documented inline allowlist exception.

Scope is limited to the checker script, focused unit tests, the compose
template edits needed for the checker to pass, and the lint-and-type CI hook.
No compose generation refactor, runtime lifecycle changes, branch changes, push,
or broad AWF/GitHub validation are part of this work.

## Requirements Checklist

- Add `scripts/ci/check_j2_tojson.py` with clear `path:line` diagnostics and
  non-zero exit on unjustified raw value interpolation.
- Discover tracked `docker/compose/*.j2` templates by default, while accepting
  explicit paths for local and test use.
- Ignore Jinja control blocks and ordinary comments for violation detection.
- Treat final approved escaping filters, initially `tojson`, as safe.
- Support inline allowlist directives keyed by template plus normalized
  expression, require a non-empty rationale, and flag stale or invalid entries.
- Update every remaining raw interpolation in
  `docker/compose/workspace.base.yml.j2` with `| tojson` or a justified
  allowlist directive.
- Wire the checker into `.github/workflows/ci.yml` under `lint-and-type`.
- Add regression coverage for failing raw interpolation and passing escaped
  interpolation, plus allowlist behavior.

## Implementation Steps

1. Write focused unit tests for checker pass/fail cases and CI workflow wiring.
2. Run the new focused tests to confirm the expected red phase.
3. Implement `scripts/ci/check_j2_tojson.py`.
4. Update `docker/compose/workspace.base.yml.j2` to satisfy the guard.
5. Wire the checker into the lint-and-type workflow.
6. Run focused tests and narrow lint/type checks for touched files only.
7. Write `plans/J2_TOJSON_GUARD_VALIDATION.md` with requirement status and
   evidence.

## Verification Commands

- `uv run --python 3.12 --extra dev pytest tests/unit/scripts/test_check_j2_tojson.py -q`
- `uv run --python 3.12 --extra dev pytest tests/unit/test_ci_workflow_full_coverage.py -q -k lint_and_type`
- `uv run --python 3.12 --extra dev python scripts/ci/check_j2_tojson.py`
- `uv run --python 3.12 --extra dev ruff check scripts/ci/check_j2_tojson.py tests/unit/scripts/test_check_j2_tojson.py tests/unit/test_ci_workflow_full_coverage.py`
- `uv run --python 3.12 --extra dev ruff format --check scripts/ci/check_j2_tojson.py tests/unit/scripts/test_check_j2_tojson.py tests/unit/test_ci_workflow_full_coverage.py`
- `uv run --python 3.12 --extra dev mypy scripts/ci/check_j2_tojson.py`

Full AWF/GitHub validation and coverage gates are intentionally left to AWF
after agent completion.
