# PRRT_kwDOSJAM6s6CNsiZ Go Mod Download Plan

## Problem Statement And Scope

Address review thread `PRRT_kwDOSJAM6s6CNsiZ` against
`src/awf/runtime/validation.py`. The setup dependency classifier currently
treats `go mod download` as a known Go command with an unsupported first
subcommand, so it returns a direct `False` and bypasses the output-based
dependency fallback. The scope is limited to classifying Go module download
setup failures as retryable dependency network failures without broadening
unrelated Go command retries.

## Requirements Checklist

- Confirm the reviewer feedback against current code before implementation.
- Add regression coverage for transient `go mod download` failures involving
  Go module network hosts.
- Preserve the existing skip behavior for non-dependency Go commands such as
  `go test ./...`.
- Keep the command matcher change minimal and scoped to nested Go module
  download handling.
- Validate with focused runtime validation tests and lint for changed files.

## Implementation Steps

1. Add a failing unit test for `go mod download` with transient Go proxy output.
2. Run the focused test to confirm the current implementation fails.
3. Extend non-uv dependency command matching to recognize the nested
   `go mod download` command.
4. Run focused and broader touched-file validation.
5. Record plan validation evidence in a matching validation document.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_validation.py -q -k "go_mod_download or non_install_package_manager_verbs"`
  passes.
- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_validation.py -q`
  passes.
- `uv run --python 3.12 --extra dev ruff check src/awf/runtime/validation.py tests/unit/runtime/test_validation.py`
  passes.
