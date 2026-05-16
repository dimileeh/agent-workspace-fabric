# Adopt PR Help CI Fix Validation

Plan reference: `plans/ADOPT_PR_HELP_CI_PLAN.md`

## Requirement Status

- Preserve `workspace adopt-pr --model` and `--effort` runtime behavior:
  Complete. The command option definitions and request body handling remain in
  place, and the adopt-pr CLI test slice passes.
- Ensure `workspace adopt-pr --help` contains literal `--model` and `--effort`
  even under narrow CI terminal widths:
  Complete. CLI import now configures a minimum Rich help width, and the
  original focused help test passes with `COLUMNS=30`.
- Add or update focused regression coverage for narrow-width help rendering:
  Complete. Added a unit test that simulates an undersized Rich help width and
  asserts the model and effort flags are exposed.
- Do not disable, skip, or weaken the failing check:
  Complete. No checks were disabled or skipped.
- Validate with the focused failing pytest node under a narrow `COLUMNS` value:
  Complete.

## Evidence

Files changed:

- `src/awf/cli/main.py`
- `tests/unit/cli/test_cli.py`
- `plans/ADOPT_PR_HELP_CI_PLAN.md`
- `plans/ADOPT_PR_HELP_CI_VALIDATION.md`

Commands run:

- `COLUMNS=30 uv run --python 3.12 --extra dev pytest tests/unit/cli/test_cli.py::TestWorkspaceAdoptPr::test_adopt_pr_help_exposes_model_and_effort_flags -q`
- `uv run --python 3.12 --extra dev pytest tests/unit/cli/test_cli.py::TestWorkspaceAdoptPr -q`
- `uv run --python 3.12 --extra dev ruff check src/awf/cli/main.py tests/unit/cli/test_cli.py`
- `uv run --python 3.12 --extra dev pytest tests/unit/cli/test_cli.py -q`
- `uv run --python 3.12 --extra dev mypy src/awf`

All commands passed.
