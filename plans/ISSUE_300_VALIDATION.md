# ISSUE-300 Validation

## Plan conformance

| Plan item | Status | Notes |
|-----------|--------|-------|
| New module `src/awf/cli/env_file.py` with `parse_dotenv_file`, `parse_env_from_arg`, `parse_env_exclude_arg` | ✅ Done | All three functions implemented and tested. |
| New module `src/awf/cli/companion_env.py` with `merge_companion_env` | ✅ Done | Merges, validates, warns, excludes. Reuses `_ENVIRONMENT_KEY_PATTERN` and `_value_has_compose_interpolation` from `schemas_companions`. |
| Modifications to `src/awf/cli/workspace_commands.py` — add `--companion-env-from` and `--companion-env-exclude` flags | ✅ Done | Both flags added; `workspace_create` calls `merge_companion_env` after building companions. |
| Export `_ENVIRONMENT_KEY_PATTERN` and `_value_has_compose_interpolation` from `schemas_companions` | ✅ Already exported | Module-level names are importable as-is; no changes needed. |
| No changes to API/server layer | ✅ Confirmed | Only CLI modules touched. |
| No changes to `openapi.json` | ✅ Confirmed | No schema changes — client-side only. |

## Acceptance criteria

| Criterion | Status | Notes |
|-----------|--------|-------|
| Example from issue works client-side | ✅ Done | `--companion-env-from` and `--companion-env-exclude` flags accept `name=path` / `name=KEYS` format, merge into companions, and exclude keys. |
| `.env` parsing: comments, quotes, blank lines, `export` prefix | ✅ Done | `test_parsed_*` tests in `test_env_file.py` cover all syntax cases. |
| Merge precedence: payload wins over file | ✅ Done | `test_payload_wins_over_file` |
| File fills gaps | ✅ Done | `test_file_fills_gaps` |
| Exclude drops keys after merge | ✅ Done | `test_exclude_drops_keys`, `test_exclude_applies_after_merge` |
| `~` / env-var path expansion | ✅ Done | `test_parse_env_from_arg_expands_home`, `test_parse_env_from_arg_expands_env_var` |
| Missing companion → error | ✅ Done | `test_missing_companion_raises`, `test_exclude_for_missing_companion_raises` |
| Missing file → clear error (exit 2, actionable message) | ✅ Done | `test_missing_env_file_raises` — raises `FileNotFoundError` with `"--companion-env-from"` context |
| **Unreadable file → clear error (exit 2, actionable message)** | ✅ Done | `test_unreadable_env_file_raises` — raises `PermissionError` with `"unreadable"` and companion name. This was a gap identified in iteration 1: the original code only caught `FileNotFoundError`; `PermissionError` would produce an unhandled traceback instead of an actionable CLI error. Fixed in both `env_file.py` (`parse_dotenv_file`) and `companion_env.py` (`merge_companion_env` early `os.access` check). |
| Malformed argument → clear error | ✅ Done | `test_parse_env_from_arg_no_equals_raises`, `test_parse_env_from_arg_empty_name_raises`, `test_parse_env_from_arg_empty_path_raises`, `test_parse_env_exclude_arg_no_equals_raises`, `test_parse_env_exclude_arg_empty_name_raises`, `test_parse_env_exclude_arg_empty_keys_raises` |
| Validation warnings show key names ONLY (never values) | ✅ Done | `test_warning_never_leaks_value` confirms `super-secret-value-12345` does not appear in stderr or stdout. |
| `--help` documents the new flags | ✅ Done | Typer `help=` text on both options. |

## Iteration 1 gap fix

**Gap**: `PermissionError` on unreadable `.env` files was not caught. `parse_dotenv_file()` returned `{}` on `FileNotFoundError` (silent drop) and `merge_companion_env()` only checked `file_path.is_file()`. A permission-denied file passes `is_file()` but `read_text()` raises `PermissionError`, producing an unhandled traceback instead of an actionable CLI error.

**Fix applied** (two files):

1. **`src/awf/cli/env_file.py`** — `parse_dotenv_file()` now raises `FileNotFoundError` (not returns `{}`) and `PermissionError` with actionable messages instead of letting them propagate as raw tracebacks. Both use `raise ... from exc` for clean traceback chaining.

2. **`src/awf/cli/companion_env.py`** — `merge_companion_env()` adds an upfront `os.access(file_path, os.R_OK)` check so that unreadable files are caught before reaching `parse_dotenv_file()`, producing: `"--companion-env-from file is unreadable (permission denied) for companion 'name': '/path/.env'"`. The docstring `Raises` section is updated to list `PermissionError` alongside `ValueError` and `FileNotFoundError`.

**Tests added**: `test_parsed_file_permission_denied_raises` in `test_env_file.py` and `test_unreadable_env_file_raises` in `test_companion_env.py`.

## Validation commands run

```bash
# Lint (focused)
uv run --python 3.12 --extra dev ruff check src/awf/cli/env_file.py src/awf/cli/companion_env.py src/awf/api/schemas_companions.py
# → All checks passed

# Format check
uv run --python 3.12 --extra dev ruff format --check src/awf/cli/env_file.py src/awf/cli/companion_env.py src/awf/api/schemas_companions.py
# → 3 files already formatted

# Type check (focused)
uv run --python 3.12 --extra dev mypy src/awf/cli/env_file.py src/awf/cli/companion_env.py
# → Success: no issues found in 2 source files

# Unit tests (focused)
uv run --python 3.12 --extra dev pytest tests/unit/cli/test_env_file.py tests/unit/cli/test_companion_env.py -v
# → 41 passed in 0.66s

# Full AWF/GitHub CI validation is managed by AWF after agent completion.
# It was NOT executed during the agent phase per AWF workspace contract rule 4.
```
