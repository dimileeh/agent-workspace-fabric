# Comment 3330046591 Owned Path Prompt Escaping Validation

Plan reference:
`plans/COMMENT_3330046591_OWNED_PATH_PROMPT_ESCAPING_PLAN.md`

## Requirement Status

- Render each declared `owned_paths` entry as quoted/escaped untrusted data in
  monitor prompts: Complete.
- Preserve the existing protected-file policy behavior and "owned protected
  paths are editable" guidance: Complete.
- Add regression coverage showing newline/control-text owned paths do not create
  standalone prompt instruction lines: Complete.
- Keep validation focused to the touched prompt unit tests and targeted lint for
  touched files: Complete.
- Record validation evidence in a matching validation document: Complete.

## Evidence

Files changed:

- `src/awf/runtime/monitor_prompts.py`
- `tests/unit/runtime/test_monitor_prompts.py`
- `plans/COMMENT_3330046591_OWNED_PATH_PROMPT_ESCAPING_PLAN.md`
- `plans/COMMENT_3330046591_OWNED_PATH_PROMPT_ESCAPING_VALIDATION.md`

Test-first evidence:

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_monitor_prompts.py -q`
  initially failed on the three new owned-path escaping regressions before the
  implementation change.

Passing focused checks:

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_monitor_prompts.py -q`
  passed: 52 passed.
- `uv run --python 3.12 --extra dev ruff check src/awf/runtime/monitor_prompts.py tests/unit/runtime/test_monitor_prompts.py`
  passed.

Full AWF/GitHub validation was not run during the agent phase per the workspace
contract; AWF owns broad validation, provenance, and merge gating after agent
completion.
