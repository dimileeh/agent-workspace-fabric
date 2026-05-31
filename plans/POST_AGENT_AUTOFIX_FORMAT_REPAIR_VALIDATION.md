# Post-Agent Autofix Format Repair Validation

## Plan

Validated against `plans/POST_AGENT_AUTOFIX_FORMAT_REPAIR_PLAN.md`.

## Results

- Added a regression test for mixed fixable `awf-ruff-check` plus
  `awf-ruff-format-check` failures in post-agent commit repair.
- Confirmed the new test failed before implementation because no `ruff format`
  invocation occurred.
- Updated `_run_post_agent_autofixable_precommit_repair` to run scoped
  `ruff format` on staged `Would reformat:` paths after `ruff check --fix` and
  before restaging/retrying `git commit`.
- Preserved the existing single-hook `ruff check --fix` behavior and existing
  repair event shape.

## Validation Commands

Red test before implementation:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor_post_agent_commit_parts/test_executor_post_agent_commit_part_001.py::test_post_agent_commit_autofixable_ruff_check_also_formats_reported_paths -q
```

Result: failed with `assert len(ruff_format_calls) == 1`.

Green focused regression after implementation:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor_post_agent_commit_parts/test_executor_post_agent_commit_part_001.py::test_post_agent_commit_autofixable_ruff_check_also_formats_reported_paths -q
```

Result: `1 passed in 1.56s`.

Focused post-agent commit suite:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor_post_agent_commit_parts/test_executor_post_agent_commit_part_001.py tests/unit/control/test_executor_post_agent_commit_classifier.py -q
```

Result: `36 passed in 31.97s`.

Lint/type checks:

```bash
uv run --python 3.12 --extra dev ruff check src/awf/control/executor/quality_methods.py tests/unit/control/test_executor_post_agent_commit_parts/test_executor_post_agent_commit_part_001.py tests/unit/control/test_executor_post_agent_commit_classifier.py
uv run --python 3.12 --extra dev ruff format --check src/awf/control/executor/quality_methods.py tests/unit/control/test_executor_post_agent_commit_parts/test_executor_post_agent_commit_part_001.py
uv run --python 3.12 --extra dev mypy src/awf/control/executor/quality_methods.py
```

Results:

- Ruff check: passed.
- Ruff format check: `2 files already formatted`.
- Mypy: `Success: no issues found in 1 source file`.

Note: an initial `ruff format --check` command included this Markdown file and
failed because Ruff Markdown formatting requires preview mode. The check was
rerun on the Python files only.

## Gaps

No known gaps for this bug fix.
