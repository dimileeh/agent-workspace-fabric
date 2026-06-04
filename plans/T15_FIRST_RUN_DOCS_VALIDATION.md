# T15 First-Run Docs Validation

Plan reference: `plans/T15_FIRST_RUN_DOCS_PLAN.md`

Source contract: `docs/awf-plans/ws_b77253c13d91444db1348fc1.md`

## Requirement Status

- Complete: Present the currently available public first-run lanes: `uv tool` /
  `pipx`, source checkout with global tool install, and source checkout with no
  global install.
- Complete: Omit the hosted curl installer lane from public first-run docs until
  the public installer, manifest, checksums, and distribution artifacts are
  published and verified.
- Complete: Explain which lanes are release-installed and which lanes are
  inspectable.
- Complete: For each lane, document setup, start, project init, mocked smoke,
  upgrade, and uninstall.
- Complete: Use current first-run grammar: `awf setup`, `awf start`,
  `awf init <path>` or project-local `awf init .`, and
  `awf smoke run --mocked-local --format pretty`.
- Complete: Keep first-run local API and console URLs aligned with the default
  smoke probe targets.
- Complete: Remove stale placeholder and no-path bootstrap language from public
  first-run docs.
- Complete: Add `docs/UNINSTALL.md` with lane-specific uninstall guidance and a
  clear distinction between CLI/source removal and destructive local state
  cleanup.
- Complete: Update focused docs tests so stale command grammar is rejected.
- Complete: Leave broad AWF/GitHub validation to post-agent infrastructure.

## Files Changed

- `README.md`
- `RELEASING.md`
- `docs/GETTING_STARTED.md`
- `docs/MCP_SETUP.md`
- `docs/PROJECT_ONBOARDING.md`
- `docs/QUICKSTART.md`
- `docs/README.md`
- `docs/SMOKE_COMMAND.md`
- `docs/UNINSTALL.md`
- `docs/UPGRADE.md`
- `plans/T15_FIRST_RUN_DOCS_PLAN.md`
- `plans/T15_FIRST_RUN_DOCS_VALIDATION.md`
- `tests/unit/cli/test_init_parts/test_init_part_004.py`
- `tests/unit/docs/test_public_docs_status.py`

## Evidence

Red-phase focused docs run before implementation failed as expected with stale
Quickstart, Getting Started, MCP setup, Project Onboarding, missing
`docs/UNINSTALL.md`, and no-path release-doc grammar failures.

Final focused validation:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/docs/test_public_docs_status.py tests/unit/docs/test_troubleshooting_guide.py tests/unit/cli/test_init_parts/test_init_part_004.py -q
```

Result: `44 passed in 1.07s`.

```bash
uv run --python 3.12 --extra dev ruff check tests/unit/docs/test_public_docs_status.py tests/unit/docs/test_troubleshooting_guide.py tests/unit/cli/test_init_parts/test_init_part_004.py
```

Result: `All checks passed!`.

```bash
uv run --python 3.12 --extra dev pytest tests/unit/docs/test_release_docs.py -q
```

Result: `3 passed in 0.40s`.

Post-review repair for PR thread `PRRT_kwDOSJAM6s6G_99y`:

```bash
curl -fsSI https://aira.pro/install.sh
```

Result: failed with HTTP `404`, confirming the hosted installer is not currently
fetchable.

```bash
uv run --python 3.12 --extra dev pytest tests/unit/docs/test_public_docs_status.py -q
```

Red-phase result after updating the focused assertions: failed with stale
README, Quickstart, Getting Started, Upgrade, and Uninstall curl-installer
advertising still present.

Final repair result: `26 passed in 0.87s`.

```bash
uv run --python 3.12 --extra dev ruff check tests/unit/docs/test_public_docs_status.py
```

Result: `All checks passed!`.

Post-review repair for review-level comment `issue:4620140358`:

- `docs/QUICKSTART.md` Lane 1 now describes `awf init <path>` as accepting any
  path, including an empty eval directory or a checked-out project.
- `tests/unit/cli/test_init_parts/test_init_part_004.py` now slices the
  Getting Started section with explicit heading assertions instead of indexed
  `str.split(...)[1]`; the same focused assertion now matches the documented
  `--project <path>` smoke command form.

```bash
uv run --python 3.12 --extra dev pytest tests/unit/cli/test_init_parts/test_init_part_004.py::test_getting_started_recommends_setup_start_then_project_init tests/unit/docs/test_public_docs_status.py::test_quickstart_presents_available_complete_first_run_lanes -q
```

Result: `2 passed in 0.45s`.

```bash
uv run --python 3.12 --extra dev ruff check tests/unit/cli/test_init_parts/test_init_part_004.py
```

Result: `All checks passed!`.

Post-review repair for PR thread `PRRT_kwDOSJAM6s6HAl67`:

- `docs/UPGRADE.md` now tells operators to reuse the project path initialized
  during first run and passes `--project <path>` to every upgrade and rollback
  smoke command.
- `tests/unit/docs/test_public_docs_status.py` now rejects bare upgrade-guide
  `smoke run` lines that would validate the current working directory.

```bash
uv run --python 3.12 --extra dev pytest tests/unit/docs/test_public_docs_status.py::test_upgrade_and_uninstall_docs_cover_all_first_run_lanes -q
```

Red-phase result after updating the focused assertion: failed because
`docs/UPGRADE.md` still used bare `awf smoke run --mocked-local --format pretty`.

Final repair result: `1 passed in 0.52s`.

```bash
uv run --python 3.12 --extra dev ruff check tests/unit/docs/test_public_docs_status.py
```

Result: `All checks passed!`.

Post-review repair for PR thread `PRRT_kwDOSJAM6s6HAx2p`:

- `docs/QUICKSTART.md` now passes the lane's initialized project path to each
  upgrade smoke command.
- `tests/unit/docs/test_public_docs_status.py` now rejects Quickstart smoke
  command lines that omit `--project`.

```bash
uv run --python 3.12 --extra dev pytest tests/unit/docs/test_public_docs_status.py::test_quickstart_is_canonical_and_not_a_stub tests/unit/docs/test_public_docs_status.py::test_quickstart_presents_available_complete_first_run_lanes tests/unit/docs/test_public_docs_status.py::test_quickstart_smoke_commands_reuse_initialized_project_paths -q
```

Red-phase result after updating the focused assertion: failed because
`docs/QUICKSTART.md` still used bare upgrade smoke commands without `--project`.

Final repair result: `3 passed in 0.54s`.

```bash
uv run --python 3.12 --extra dev pytest tests/unit/docs/test_public_docs_status.py -q
```

Result: `27 passed in 0.91s`.

```bash
uv run --python 3.12 --extra dev ruff check tests/unit/docs/test_public_docs_status.py
```

Result: `All checks passed!`.

Post-review repair for PR thread `PRRT_kwDOSJAM6s6HBU4r`:

- `docs/QUICKSTART.md` now leaves the `AWF_GITHUB_TOKEN` export commented and
  explicitly optional in all three mocked first-run lane command blocks.
- `tests/unit/docs/test_public_docs_status.py` now rejects active Quickstart
  `gh auth token` exports while requiring the optional commented guidance in
  each lane.

```bash
uv run --python 3.12 --extra dev pytest tests/unit/docs/test_public_docs_status.py::test_quickstart_mocked_smoke_keeps_github_auth_optional -q
```

Red-phase result after updating the focused assertion: failed because
`docs/QUICKSTART.md` still hard-required
`export AWF_GITHUB_TOKEN="$(gh auth token)"`.

```bash
uv run --python 3.12 --extra dev pytest tests/unit/docs/test_public_docs_status.py::test_quickstart_mocked_smoke_keeps_github_auth_optional tests/unit/docs/test_public_docs_status.py::test_quickstart_presents_available_complete_first_run_lanes tests/unit/docs/test_public_docs_status.py::test_quickstart_is_canonical_and_not_a_stub -q
```

Final repair result: `3 passed in 0.55s`.

```bash
uv run --python 3.12 --extra dev ruff check tests/unit/docs/test_public_docs_status.py
```

Result: `All checks passed!`.

```bash
uv run --python 3.12 --extra dev ruff format --check tests/unit/docs/test_public_docs_status.py
```

Result: `1 file already formatted`.

Post-review repair for PR thread `PRRT_kwDOSJAM6s6HBU-B`:

- `docs/QUICKSTART.md` now documents the actual default mocked-smoke probe
  targets: `http://localhost:8000` for API checks and
  `http://localhost:3000` for the default console probe.
- `tests/unit/docs/test_public_docs_status.py` now rejects Quickstart first-run
  URL prose that diverges from `DEFAULT_LOCAL_SERVICE_API_BASE_URL` and
  `DEFAULT_LOCAL_CONSOLE_URL`.

```bash
uv run --python 3.12 --extra dev pytest tests/unit/docs/test_public_docs_status.py::test_quickstart_first_run_urls_match_smoke_defaults -q
```

Red-phase result after adding the focused assertion: failed because
`docs/QUICKSTART.md` still documented `http://127.0.0.1:8000` and
`http://127.0.0.1:3000`.

```bash
uv run --python 3.12 --extra dev pytest tests/unit/docs/test_public_docs_status.py::test_quickstart_first_run_urls_match_smoke_defaults tests/unit/docs/test_public_docs_status.py::test_quickstart_is_canonical_and_not_a_stub tests/unit/docs/test_public_docs_status.py::test_quickstart_presents_available_complete_first_run_lanes -q
```

Final repair result: `3 passed in 0.56s`.

```bash
uv run --python 3.12 --extra dev ruff check tests/unit/docs/test_public_docs_status.py
```

Result: `All checks passed!`.

Full AWF/GitHub validation, full coverage, OpenAPI drift checks, and frontend
validation were intentionally not run in the agent phase; AWF owns those broad
gates after agent completion.

## Gaps

None.
