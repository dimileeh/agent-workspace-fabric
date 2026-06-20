# PRRT_kwDOSJAM6s6K5eEw Validation

Plan reference: `PRRT_kwDOSJAM6s6K5eEw_PLAN.md`

## Requirement Status

- Add a regression test for an unrecognized absolute mirror `core.hooksPath`:
  Complete. `test_clears_unrecognized_absolute_hooks_path` covers
  `/tmp/empty-hooks`.
- Repair unrecognized absolute mirror hooks paths with the existing unset flow:
  Complete. `_mirror_hooks_path_unset_pattern` now returns an exact unset
  regex for empty or absolute values outside the known poisoned table.
- Preserve the existing behavior for legitimate relative project hook paths:
  Complete. The focused test class still includes
  `test_preserves_legitimate_hooks_path`.
- Keep duplicate/concurrent cleanup behavior and reason-code failures intact:
  Complete. The full focused repair test class passed.

## Evidence

Files changed:

- `src/awf/node/git_manager.py`
- `tests/unit/node/test_git_manager.py`
- `plans/PRRT_kwDOSJAM6s6K5eEw_PLAN.md`
- `plans/PRRT_kwDOSJAM6s6K5eEw_VALIDATION.md`

Commands run:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/node/test_git_manager.py::TestRepairMirrorHooksPath::test_clears_unrecognized_absolute_hooks_path -q
```

Initial red result before implementation: failed with `assert False is True`.

```bash
uv run --python 3.12 --extra dev pytest tests/unit/node/test_git_manager.py::TestRepairMirrorHooksPath -q
```

Result after formatting: `10 passed in 0.60s`.

```bash
uv run --python 3.12 --extra dev ruff check src/awf/node/git_manager.py tests/unit/node/test_git_manager.py
```

Result: `All checks passed!`.

```bash
uv run --python 3.12 --extra dev ruff format src/awf/node/git_manager.py tests/unit/node/test_git_manager.py
```

Result: `2 files reformatted`.

Full AWF/GitHub validation is managed by AWF after agent completion.
