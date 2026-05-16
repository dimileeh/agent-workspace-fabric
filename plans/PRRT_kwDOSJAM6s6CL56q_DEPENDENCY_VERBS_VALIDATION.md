# PRRT_kwDOSJAM6s6CL56q Dependency Verb Validation

Plan reference: `plans/PRRT_kwDOSJAM6s6CL56q_DEPENDENCY_VERBS_PLAN.md`

## Requirement Status

- Confirm the feedback against current code before editing: Complete.
  Added regression tests first and confirmed the current implementation failed
  for `npm run build`, `poetry run ...`, `bundle exec ...`, and `go test ...`.
- Add regression coverage proving non-install setup commands are not classified:
  Complete. `tests/unit/runtime/test_validation.py` now covers known
  package-manager non-install verbs.
- Preserve existing uv-specific behavior, including skipping `uv run`: Complete.
  The existing `uv run` regression remains in the same test file and passes.
- Preserve classification for real dependency setup commands: Complete.
  Added coverage for `npm ci`, `poetry install`, `bundle install`,
  `go install`, Gradle dependency resolution, Maven go-offline, and
  `python -m pip install`.
- Keep the change minimal and do not alter unrelated retry behavior: Complete.
  The change is limited to setup dependency command classification and focused
  unit tests.
- Validate with the narrowest relevant command: Complete.

## Evidence

Files changed:

- `src/awf/runtime/validation.py`
- `tests/unit/runtime/test_validation.py`
- `plans/PRRT_kwDOSJAM6s6CL56q_DEPENDENCY_VERBS_PLAN.md`
- `plans/PRRT_kwDOSJAM6s6CL56q_DEPENDENCY_VERBS_VALIDATION.md`

Commands run:

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_validation.py -q`
  initially failed with 4 expected regression failures before implementation.
- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_validation.py -q`
  passed with `165 passed`.
- `uv run --python 3.12 --extra dev ruff check src/awf/runtime/validation.py tests/unit/runtime/test_validation.py`
  passed.
- `uv run --python 3.12 --extra dev mypy src/awf` passed.

## Gaps

None.
