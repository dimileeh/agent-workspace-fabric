# ISSUE-300: Add --companion-env-from and --companion-env-exclude to workspace create

## Goal

Add client-side CLI flags that read a companion's local `.env` file and merge
its variables into that companion's `environment` block, avoiding hand-copying
secrets and reducing drift.

## Scope

- **CLI only** — the API server never sees `.env` file paths; merging happens
  in the CLI while building the request body.
- **New flags**:
  - `--companion-env-from <name>=<path>` (repeatable)
  - `--companion-env-exclude <name>=<KEY1,KEY2,...>` (repeatable)

## Design decisions

1. **`.env` parsing** — write our own `parse_dotenv_file()` in a new module
   `src/awf/cli/env_file.py` rather than using `dotenv_values()` directly.
   Reason: `dotenv_values()` silently handles multi-line values, variable
   substitution, and other edge cases that differ from the simple contract we
   want. Our parser documents exactly what is and isn't supported (see the
   issue contract). We still import `python-dotenv` as a dependency because the
   service layer uses it; we just don't use it for this client-side feature.

2. **Flag format** — `name=path` pairs let us match against the companion
   `name` field, so the merge is unambiguous even with multiple companions.

3. **Merge precedence** — explicit `--companion-json` values WIN over file
   values. `--companion-env-from` fills gaps; it never clobbers.

4. **Validation** — before submitting, check each merged key against AWF's
   existing `_ENVIRONMENT_KEY_PATTERN` and `_value_has_compose_interpolation`.
   On violation, emit a **warning** to stderr with the key name ONLY (never
   the value) and **skip** that key. Deterministic warn-and-skip keeps the
   CLI usable without silently dropping data.

5. **Error handling** — non-zero exit + actionable message for:
   - `--companion-env-from` names a companion not in any `--companion-json`
   - File path does not exist or is unreadable
   - Malformed `name=path` / `name=KEYS` argument

6. **Security** — never print/log/env-dump parsed `.env` values. Validation
   warnings show key names only. Use `redact_secrets()` if ever surfacing
   derived info (though we don't expect to).

## Implementation plan

### Step 1 — New module `src/awf/cli/env_file.py`

- `parse_dotenv_file(path: Path) -> dict[str, str]`
  - Parses `KEY=value` lines
  - Strips `#` comment lines and blank lines
  - Strips a leading `export ` prefix
  - Strips surrounding `"..."` / `'...'` quotes
  - Does NOT support: multi-line values, `${VAR}` substitution within
    the file, `source` directives, or any Docker Compose interpolation.
- `parse_env_from_arg(arg: str) -> tuple[str, str]`
  - Parses `name=path` format, expands `~` and `$ENV_VAR` in path
- `parse_env_exclude_arg(arg: str) -> tuple[str, set[str]]`
  - Parses `name=KEY1,KEY2,...` format

### Step 2 — New module `src/awf/cli/companion_env.py`

- `merge_companion_env(
    companions: list[dict[str, Any]],
    env_from: Sequence[tuple[str, str]],
    env_exclude: Sequence[tuple[str, str]],
  ) -> list[dict[str, Any]]`
  - Reads .env files, merges into matching companions, applies excludes,
    validates keys/values, warns and skips invalid entries.
  - Reuses `_ENVIRONMENT_KEY_PATTERN` and `_value_has_compose_interpolation`
    from `src/awf/api/schemas_companions.py`.

### Step 3 — Modify `src/awf/cli/workspace_commands.py`

- Add `companion_env_from` and `companion_env_exclude` options to
  `workspace_create`.
- After building `companions` from `--companion-json`, call
  `merge_companion_env` to apply the file-based merges.

### Step 4 — Tests in `tests/unit/cli/`

- `test_env_file.py` — unit tests for `parse_dotenv_file`
- `test_companion_env.py` — unit tests for `merge_companion_env`
- Add cases to `test_workspace_commands_helpers.py` for end-to-end CLI

## Files changed

- `src/awf/cli/env_file.py` (new)
- `src/awf/cli/companion_env.py` (new)
- `src/awf/cli/workspace_commands.py` (modify — add flags + merge call)
- `tests/unit/cli/test_env_file.py` (new)
- `tests/unit/cli/test_companion_env.py` (new)
- `tests/unit/cli/test_workspace_commands_helpers.py` (modify — add cases)

## Out of scope

- AWF does NOT read its own service `.env`; this is client-side only.
- No secret storage/management features.
- No changes to the API server or schemas.
