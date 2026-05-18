# Plan: Address PR #264 review comment 4314143870

## Problem statement and scope
- Address CodeRabbit review feedback for `awf init` environment file handling.
- Scope is limited to the bootstrap-mode CLI behavior and focused unit coverage:
  - `src/awf/cli/main.py`
  - `tests/unit/cli/test_init.py`
  - this plan/validation pair

## Requirements checklist
- [ ] Help text for `--write-env` explicitly names the Compose env target path `docker/compose/.env`.
- [ ] Missing `.env.example` output reports the actual example paths searched, including Compose and fallback locations when Compose assets are active.
- [ ] Copying `.env.example` to the target handles filesystem errors without an unclear traceback.
- [ ] On write failure, pretty output emits a clear warning naming the target, source, and error.
- [ ] On write failure, JSON output records `env_action` as `write_failed`.

## Implementation steps
- Add focused regression tests for help text, missing-example search paths, pretty write-failure output, and JSON `env_action`.
- Update `src/awf/cli/main.py` with localized help text, missing-example message formatting, and guarded env file copy.
- Run the narrow CLI init unit tests and lint for the touched Python files.

## Verification commands and pass criteria
- `uv run --python 3.12 --extra dev pytest tests/unit/cli/test_init.py -k "write_env_help or compose_env_examples_missing or env_write_fails" -q`
  - Pass: targeted regressions are green.
- `uv run --python 3.12 --extra dev pytest tests/unit/cli/test_init.py -q`
  - Pass: all init CLI tests are green.
- `uv run --python 3.12 --extra dev ruff check src/awf/cli/main.py tests/unit/cli/test_init.py`
  - Pass: no lint issues in touched Python files.
