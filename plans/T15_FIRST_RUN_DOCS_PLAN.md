# T15 First-Run Docs Plan

## Scope

Implement T15 from `TODO/awf-full-installer-first-run-setup-backlog.md` with a
small documentation-only change:

- Update README, Quickstart, Getting Started, Upgrade, Uninstall, docs index,
  and release notes for first-run lanes.
- Present the three currently runnable lanes completely:
  - `uv tool` / `pipx` release-installed package.
  - source checkout with global `uv tool install . --force`.
  - source checkout with no global install via `uv run`.
- Keep the curl installer lane visible as release-gated until hosted installer
  URL, manifest, checksums, and release artifacts are published and verified.
- Document setup, start, `awf init <path>`, mocked smoke, upgrade, and uninstall
  paths.
- Use `127.0.0.1` for local API and console URLs where applicable.
- Add focused docs tests for lane coverage, uninstall discovery, stale
  no-path-init language, and the absence of generated dotenv parser blocks.

## Out Of Scope

- CLI/runtime behavior changes.
- Installer implementation changes.
- Workflow, package, OpenAPI, frontend, or lockfile changes.
- Publicly advertising `curl | bash` before release hosting is actually ready.

## Validation

Run focused docs and maintainability validation:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/docs/test_public_docs_status.py -q
uv run --python 3.12 --extra dev pytest tests/unit/cli/test_init_parts/test_init_part_004.py::test_getting_started_recommends_setup_start_then_project_init -q
uv run --python 3.12 --extra dev pytest tests/unit/test_core_decomposition_maintainability.py::test_first_party_code_files_stay_under_line_limit -q
uv run --python 3.12 --extra dev ruff check tests/unit/docs/test_public_docs_status.py tests/unit/cli/test_init_parts/test_init_part_004.py
```
