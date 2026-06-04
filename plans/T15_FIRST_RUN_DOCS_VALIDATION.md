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
- Complete: Address review-level comment `issue:4620140358` by documenting the
  README-supported virtualenv/pip lifecycle path and tightening focused test
  helper diagnostics.
- Complete: Address PR thread `PRRT_kwDOSJAM6s6HB-xB` by preventing source
  checkout uninstall guidance from deleting a checkout while persisted
  `source_checkout` metadata still points at it.
- Complete: Address PR thread `PRRT_kwDOSJAM6s6HCN6b` by making the
  self-contained Quickstart source-checkout/no-global-install uninstall lane
  clear or refresh persisted `source_checkout` metadata before deleting the
  recorded checkout.
- Complete: Address PR thread `PRRT_kwDOSJAM6s6HCmnm` by making the Quickstart
  source-checkout/global-tool uninstall lane clear or refresh persisted
  `source_checkout` metadata before deleting the recorded checkout.
- Complete: Address review-level comment `issue:4620140358` by removing the
  Getting Started `127.0.0.1` first-run URL contradiction and making the
  Quickstart optional GitHub token assertion self-calibrate from lane headings.
- Complete: Address PR thread `PRRT_kwDOSJAM6s6HE3SX` by scoping README bare
  `awf` first-run commands to lanes that put `awf` on `PATH` and documenting
  the no-global `uv run --python 3.12 --extra dev awf ...` wrapper alongside
  them.
- Complete: Address PR thread `PRRT_kwDOSJAM6s6HFXeN` by stopping local Core
  before source-checkout upgrade snippets refresh persisted `source_checkout`
  metadata with `awf setup --source-checkout "$PWD"`.
- Complete: Address PR thread `PRRT_kwDOSJAM6s6HHCBV` by preventing package and
  virtualenv upgrade snippets from generating a replacement `AWF_API_TOKEN` when
  `.env` does not already persist the running local Core token.
- Complete: Address PR thread `PRRT_kwDOSJAM6s6HHCfa` by restoring mandatory
  package and virtualenv service env before the release-installed rollback
  `awf start` command in `docs/UPGRADE.md`.
- Complete: Address review-level comment `issue:4620140358` by adding guarded
  Core stop commands before every `docs/UNINSTALL.md` source-checkout metadata
  refresh example.
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

Post-review repair for review-level comment `issue:4620140358`:

- `docs/UPGRADE.md` now documents the virtualenv/pip upgrade path advertised by
  README with `pip install --upgrade agent-workspace-fabric`.
- `docs/UNINSTALL.md` now documents the matching virtualenv/pip uninstall path
  with `pip uninstall agent-workspace-fabric`.
- `tests/unit/docs/test_public_docs_status.py` now rejects H3-or-deeper
  `_markdown_section` calls with `ValueError` and gives the hosted curl
  omission assertion a document-specific failure message.

```bash
uv run --python 3.12 --extra dev pytest tests/unit/docs/test_public_docs_status.py::test_markdown_section_rejects_h3_or_deeper_headings tests/unit/docs/test_public_docs_status.py::test_virtualenv_lifecycle_docs_cover_readme_install_path -q
```

Red-phase result after adding the focused assertions: failed because
`_markdown_section` did not raise `ValueError` for an H3 heading and
`docs/UPGRADE.md` lacked `## Virtualenv / pip`.

```bash
uv run --python 3.12 --extra dev pytest tests/unit/docs/test_public_docs_status.py::test_markdown_section_rejects_h3_or_deeper_headings tests/unit/docs/test_public_docs_status.py::test_virtualenv_lifecycle_docs_cover_readme_install_path tests/unit/docs/test_public_docs_status.py::test_quickstart_presents_available_complete_first_run_lanes tests/unit/docs/test_public_docs_status.py::test_upgrade_and_uninstall_docs_cover_all_first_run_lanes -q
```

Final repair result: `5 passed in 0.64s`.

```bash
uv run --python 3.12 --extra dev pytest tests/unit/docs/test_public_docs_status.py -q
```

Result: `33 passed in 0.95s`.

```bash
uv run --python 3.12 --extra dev ruff check tests/unit/docs/test_public_docs_status.py
```

Result: `All checks passed!`.

Post-review repair for PR thread `PRRT_kwDOSJAM6s6HB-xB`:

- `docs/UNINSTALL.md` now explains that `awf setup --source-checkout` persists
  source checkout metadata in `~/.awf/config.yml`, and that operators must
  refresh the persisted path or remove only the top-level `source_checkout:`
  block before deleting the checkout.
- `tests/unit/docs/test_public_docs_status.py` now rejects uninstall docs that
  list `rm -rf` before the persisted source-checkout config guidance.

```bash
uv run --python 3.12 --extra dev pytest tests/unit/docs/test_public_docs_status.py::test_upgrade_and_uninstall_docs_cover_all_first_run_lanes -q
```

Red-phase result after adding the focused assertion: failed because
`docs/UNINSTALL.md` did not mention `~/.awf/config.yml` before the checkout
deletion command.

Final repair result: `1 passed in 0.57s`.

```bash
uv run --python 3.12 --extra dev ruff check tests/unit/docs/test_public_docs_status.py
```

Result: `All checks passed!`.

Post-review repair for PR thread `PRRT_kwDOSJAM6s6HCN6b`:

- `docs/QUICKSTART.md` Lane 3 now tells operators to refresh the persisted
  `source_checkout` path or remove only the top-level `source_checkout:` block
  from `~/.awf/config.yml` before deleting the recorded checkout.
- `tests/unit/docs/test_public_docs_status.py` now rejects the Quickstart Lane 3
  uninstall lane when checkout deletion appears without prior persisted
  source-checkout metadata cleanup guidance.

```bash
uv run --python 3.12 --extra dev pytest tests/unit/docs/test_public_docs_status.py::test_quickstart_clears_source_checkout_metadata_before_checkout_deletion -q
```

Red-phase result after adding the focused assertion: failed because
`docs/QUICKSTART.md` did not mention `~/.awf/config.yml` before the checkout
deletion command.

Final repair result: `1 passed in 0.58s`.

```bash
uv run --python 3.12 --extra dev pytest tests/unit/docs/test_public_docs_status.py -q
```

Result: `34 passed in 1.00s`.

```bash
uv run --python 3.12 --extra dev ruff check tests/unit/docs/test_public_docs_status.py
```

Result: `All checks passed!`.

Post-review repair for review-level comment `issue:4620140358`:

- `docs/GETTING_STARTED.md` no longer states that first-run local API and
  console URLs use `127.0.0.1`; it now defers first-run probe URL defaults to
  Quickstart's smoke-default wording.
- `tests/unit/docs/test_public_docs_status.py` now rejects the old
  `127.0.0.1` host-facing loopback sentence in the Getting Started first-run
  section while keeping Quickstart tied to the smoke defaults.
- `test_quickstart_mocked_smoke_keeps_github_auth_optional` now derives its
  expected optional-token comment count from `## Lane ...` headings and reports
  the lane heading when a lane is missing the comments.

```bash
uv run --python 3.12 --extra dev pytest tests/unit/docs/test_public_docs_status.py::test_quickstart_first_run_urls_match_smoke_defaults tests/unit/docs/test_public_docs_status.py::test_quickstart_mocked_smoke_keeps_github_auth_optional -q
```

Red-phase result after adding the focused assertions: failed because
`docs/GETTING_STARTED.md` still said `awf start` reported URLs using
`127.0.0.1` for host-facing loopback.

Final repair result: `2 passed in 0.57s`.

```bash
uv run --python 3.12 --extra dev pytest tests/unit/docs/test_public_docs_status.py -q
```

Result: `34 passed in 0.98s`.

```bash
uv run --python 3.12 --extra dev ruff check tests/unit/docs/test_public_docs_status.py
```

Result: `All checks passed!`.

Post-review repair for PR thread `PRRT_kwDOSJAM6s6HCmnm`:

- `docs/QUICKSTART.md` Lane 2 now tells operators to refresh the persisted
  `source_checkout` path or remove only the top-level `source_checkout:` block
  from `~/.awf/config.yml` before deleting the recorded checkout.
- `tests/unit/docs/test_public_docs_status.py` now rejects either
  source-checkout Quickstart uninstall lane when checkout deletion appears
  without prior persisted source-checkout metadata cleanup guidance.

```bash
uv run --python 3.12 --extra dev pytest tests/unit/docs/test_public_docs_status.py::test_quickstart_clears_source_checkout_metadata_before_checkout_deletion -q
```

Red-phase result after extending the focused assertion: failed because
`docs/QUICKSTART.md` Lane 2 did not mention `~/.awf/config.yml` before checkout
deletion guidance.

Final repair result: `1 passed in 0.57s`.

```bash
uv run --python 3.12 --extra dev ruff check tests/unit/docs/test_public_docs_status.py
```

Result: `All checks passed!`.

Full AWF/GitHub validation, full coverage, OpenAPI drift checks, and frontend
validation were intentionally not run in the agent phase; AWF owns those broad
gates after agent completion.

Post-review repair for PR thread `PRRT_kwDOSJAM6s6HC6Eb`:

- `docs/QUICKSTART.md` source-checkout upgrade snippets now run
  `awf setup --source-checkout "$PWD"` after `uv tool install . --force`, or
  the `uv run` equivalent after `uv sync --extra dev`, before starting from the
  checkout.
- `docs/UPGRADE.md` applies the same metadata refresh in both source-checkout
  upgrade lanes.
- `tests/unit/docs/test_public_docs_status.py` now rejects source-checkout
  upgrade snippets that start from the explicit checkout without first
  refreshing persisted `source_checkout` metadata after upgrade.

```bash
uv run --python 3.12 --extra dev pytest tests/unit/docs/test_public_docs_status.py::test_source_checkout_upgrade_docs_refresh_persisted_metadata -q
```

Red-phase result after adding the focused assertion: failed because
`docs/QUICKSTART.md` Lane 2 did not refresh `source_checkout` metadata before
`awf start --source-checkout "$PWD"`.

Final repair result after formatting the edited docs test file:
`1 passed in 0.58s`.

```bash
uv run --python 3.12 --extra dev pytest tests/unit/docs/test_public_docs_status.py -q
```

Result after formatting the edited docs test file: `35 passed in 1.05s`.

```bash
uv run --python 3.12 --extra dev ruff check tests/unit/docs/test_public_docs_status.py
```

Result: `All checks passed!`.

Full AWF/GitHub validation, full coverage, OpenAPI drift checks, and frontend
validation were intentionally not run in the agent phase; AWF owns those broad
gates after agent completion.

Post-review repair for PR thread `PRRT_kwDOSJAM6s6HDHwd`:

- `docs/QUICKSTART.md` Lane 1 now separates `uv tool` and `pipx` install,
  upgrade, and uninstall alternatives into distinct bash blocks, with shared
  setup/start/smoke commands in their own blocks.
- `tests/unit/docs/test_public_docs_status.py` now rejects Lane 1 bash blocks
  that would execute both package-manager alternatives from one copied block.

```bash
uv run --python 3.12 --extra dev pytest tests/unit/docs/test_public_docs_status.py::test_quickstart_keeps_package_manager_alternatives_in_separate_blocks -q
```

Red-phase result after adding the focused assertion: failed because
`docs/QUICKSTART.md` Lane 1 had executable `uv tool` and `pipx` install,
upgrade, and uninstall alternatives in the same bash blocks.

Final repair result: `1 passed in 0.58s`.

```bash
uv run --python 3.12 --extra dev pytest tests/unit/docs/test_public_docs_status.py::test_quickstart_presents_available_complete_first_run_lanes tests/unit/docs/test_public_docs_status.py::test_quickstart_keeps_package_manager_alternatives_in_separate_blocks tests/unit/docs/test_public_docs_status.py::test_quickstart_smoke_commands_reuse_initialized_project_paths tests/unit/docs/test_public_docs_status.py::test_quickstart_mocked_smoke_keeps_github_auth_optional -q
```

Result: `4 passed in 0.65s`.

```bash
uv run --python 3.12 --extra dev ruff check tests/unit/docs/test_public_docs_status.py
```

Result: `All checks passed!`.

Full AWF/GitHub validation, full coverage, OpenAPI drift checks, and frontend
validation were intentionally not run in the agent phase; AWF owns those broad
gates after agent completion.

Post-review repair for PR thread `PRRT_kwDOSJAM6s6HE3SX`:

- `README.md` now limits the bare first-run command block to lanes that put
  `awf` on `PATH`.
- `README.md` now gives the source-checkout/no-global-install lane a matching
  first-run block using `uv run --python 3.12 --extra dev awf ...`.
- `tests/unit/docs/test_public_docs_status.py` now rejects README first-run
  prose that advertises bare `awf` commands for every lane or omits the
  no-global wrapper.

```bash
uv run --python 3.12 --extra dev pytest tests/unit/docs/test_public_docs_status.py::test_readme_first_run_grammar_reuses_initialized_project_path -q
```

Red-phase result after tightening the focused assertion: failed because
`README.md` still said `After installing in any lane` before a bare `awf`
command block.

Final repair result: `1 passed in 0.57s`.

```bash
uv run --python 3.12 --extra dev ruff check tests/unit/docs/test_public_docs_status.py
```

Result: `All checks passed!`.

Full AWF/GitHub validation, full coverage, OpenAPI drift checks, and frontend
validation were intentionally not run in the agent phase; AWF owns those broad
gates after agent completion.

Post-review repair for PR thread `PRRT_kwDOSJAM6s6HFJSc`:

- `docs/GETTING_STARTED.md` now leaves the Recommended First-Run Sequence
  GitHub token guidance commented and optional for PR creation/monitoring,
  matching Quickstart's mocked-smoke path.
- `tests/unit/docs/test_public_docs_status.py` now rejects a Getting Started
  mocked first-run section that omits optional token guidance or hard-requires
  `export AWF_GITHUB_TOKEN="$(gh auth token)"` before mocked smoke.

```bash
uv run --python 3.12 --extra dev pytest tests/unit/docs/test_public_docs_status.py::test_getting_started_mocked_smoke_keeps_github_auth_optional -q
```

Red-phase result after adding the focused assertion: failed because
`docs/GETTING_STARTED.md` did not include optional GitHub token guidance in the
Recommended First-Run Sequence while the command block still hard-required
`gh auth token`.

Final repair result: `1 passed in 0.58s`.

```bash
uv run --python 3.12 --extra dev pytest tests/unit/docs/test_public_docs_status.py::test_getting_started_mocked_smoke_keeps_github_auth_optional tests/unit/docs/test_public_docs_status.py::test_getting_started_uses_runnable_startup_path tests/unit/docs/test_public_docs_status.py::test_quickstart_mocked_smoke_keeps_github_auth_optional -q
```

Result: `3 passed in 0.60s`.

```bash
uv run --python 3.12 --extra dev ruff check tests/unit/docs/test_public_docs_status.py
```

Result: `All checks passed!`.

Full AWF/GitHub validation, full coverage, OpenAPI drift checks, and frontend
validation were intentionally not run in the agent phase; AWF owns those broad
gates after agent completion.

Post-review repair for PR thread `PRRT_kwDOSJAM6s6HFXeN`:

- `docs/QUICKSTART.md` and `docs/UPGRADE.md` now stop the local Core Compose
  stack before source-checkout upgrade snippets run `awf setup --source-checkout
  "$PWD"`, avoiding the API/Postgres occupied-port readiness blocker.
- `tests/unit/docs/test_public_docs_status.py` now rejects source-checkout
  upgrade docs that refresh metadata before stopping local Core.

```bash
uv run --python 3.12 --extra dev pytest tests/unit/docs/test_public_docs_status.py::test_source_checkout_upgrade_docs_refresh_persisted_metadata -q
```

Red-phase result after adding the focused assertion: failed because Quickstart
Lane 2 did not stop local Core before `awf setup --source-checkout "$PWD"`.

Final repair result: `1 passed in 0.59s`.

```bash
uv run --python 3.12 --extra dev pytest tests/unit/docs/test_public_docs_status.py -q
```

Result: `42 passed in 1.12s`.

```bash
uv run --python 3.12 --extra dev ruff check tests/unit/docs/test_public_docs_status.py
```

Result: `All checks passed!`.

```bash
uv run --python 3.12 --extra dev ruff format --check tests/unit/docs/test_public_docs_status.py
```

Result: `1 file already formatted`.

Full AWF/GitHub validation, full coverage, OpenAPI drift checks, and frontend
validation were intentionally not run in the agent phase; AWF owns those broad
gates after agent completion.

Post-review repair for PR thread `PRRT_kwDOSJAM6s6HFn6N`:

- `docs/QUICKSTART.md` and `docs/UPGRADE.md` now guard source-checkout upgrade
  stop commands so `docker/compose/.env` is passed to Docker Compose only when
  it exists.
- The same snippets now provide a fallback `docker compose -f
  docker/compose/local-service.yml stop` command, matching source-checkout
  first-run lanes that exported shell values without creating the compose env
  file.
- `tests/unit/docs/test_public_docs_status.py` now rejects source-checkout
  upgrade docs that require the compose env file without a no-env-file fallback.

```bash
uv run --python 3.12 --extra dev pytest tests/unit/docs/test_public_docs_status.py::test_source_checkout_upgrade_docs_refresh_persisted_metadata -q
```

Red-phase result after adding the focused assertion: failed because Quickstart
Lane 2 did not guard the optional compose env file before stopping Core.

Final repair result: `1 passed in 0.58s`.

```bash
uv run --python 3.12 --extra dev pytest tests/unit/docs/test_public_docs_status.py -q
```

Result: `42 passed in 1.15s`.

```bash
uv run --python 3.12 --extra dev ruff check tests/unit/docs/test_public_docs_status.py
```

Result: `All checks passed!`.

Full AWF/GitHub validation, full coverage, OpenAPI drift checks, and frontend
validation were intentionally not run in the agent phase; AWF owns those broad
gates after agent completion.

Post-review repair for PR thread `PRRT_kwDOSJAM6s6HF4n7`:

- `docs/UPGRADE.md` now separates release-installed/virtualenv rollback from
  the source-checkout/global-tool rollback lane.
- The source-checkout/global-tool rollback lane now reinstalls the global tool
  from the restored checkout, stops local Core with the same optional
  `docker/compose/.env` guard used by source upgrade snippets, refreshes
  persisted source-checkout metadata with `awf setup --source-checkout "$PWD"`,
  then starts with `awf start --source-checkout "$PWD"`.
- `tests/unit/docs/test_public_docs_status.py` now rejects global-tool
  source-checkout rollback docs that omit the metadata refresh, and the
  existing no-global rollback test now scopes its assertions to the no-global
  subsection.

```bash
uv run --python 3.12 --extra dev pytest tests/unit/docs/test_public_docs_status.py::test_upgrade_global_source_checkout_rollback_refreshes_metadata -q
```

Red-phase result after adding the focused assertion: failed because
`docs/UPGRADE.md` did not contain a source-checkout/global-tool rollback block
that refreshed persisted metadata before start.

```bash
uv run --python 3.12 --extra dev pytest tests/unit/docs/test_public_docs_status.py::test_upgrade_global_source_checkout_rollback_refreshes_metadata tests/unit/docs/test_public_docs_status.py::test_upgrade_no_global_source_checkout_rollback_uses_uv_run -q
```

Final focused rollback result: `2 passed in 0.64s`.

```bash
uv run --python 3.12 --extra dev pytest tests/unit/docs/test_public_docs_status.py -q
```

Result: `43 passed in 1.19s`.

```bash
uv run --python 3.12 --extra dev ruff check tests/unit/docs/test_public_docs_status.py
```

Result: `All checks passed!`.

Full AWF/GitHub validation, full coverage, OpenAPI drift checks, and frontend
validation were intentionally not run in the agent phase; AWF owns those broad
gates after agent completion.

Post-review repair for PR thread `PRRT_kwDOSJAM6s6HGN4c`:

- `docs/QUICKSTART.md` now restores package-lane service env before the upgrade
  restart when `AWF_API_TOKEN` or `AWF_POSTGRES_PASSWORD` is not already
  persisted in `.env`.
- `docs/UPGRADE.md` now applies the same guarded env restoration to the `uv
  tool`, `pipx`, and virtualenv/pip upgrade snippets before `awf start`.
- `tests/unit/docs/test_public_docs_status.py` now rejects package upgrade docs
  that restart AWF without first preserving existing `.env` values and exporting
  missing mandatory service variables.

```bash
uv run --python 3.12 --extra dev pytest tests/unit/docs/test_public_docs_status.py::test_package_upgrade_docs_restore_service_env_before_start -q
```

Red-phase result after adding the focused assertion: failed because Quickstart
Lane 1 restarted AWF without checking `.env` or exporting `AWF_API_TOKEN`.

Final focused repair result: `1 passed in 0.65s`.

```bash
uv run --python 3.12 --extra dev pytest tests/unit/docs/test_public_docs_status.py -q
```

Result: `44 passed in 1.28s`.

```bash
uv run --python 3.12 --extra dev ruff check tests/unit/docs/test_public_docs_status.py
```

Result: `All checks passed!`.

Full AWF/GitHub validation, full coverage, OpenAPI drift checks, and frontend
validation were intentionally not run in the agent phase; AWF owns those broad
gates after agent completion.

Post-review repair for review comment `issue:4620140358`:

- `tests/unit/docs/test_public_docs_status.py` now anchors package-upgrade guard
  closure detection to a full-line shell `fi` keyword and includes a regression
  that lowercase `fi` in unrelated text is ignored.
- `docs/MCP_SETUP.md` now asks users to run
  `awf service status --format pretty` immediately after each `awf start`
  prerequisite snippet, giving a clear local Core readiness check before MCP
  registration.

```bash
uv run --python 3.12 --extra dev pytest tests/unit/docs/test_public_docs_status.py::test_mcp_setup_prerequisites_use_runnable_startup_path tests/unit/docs/test_public_docs_status.py::test_package_upgrade_env_restore_detects_only_closing_fi_keyword -q
```

Red-phase result after updating the focused assertions: failed because
`docs/MCP_SETUP.md` did not contain the status command and the helper accepted
lowercase `fi` inside unrelated text as a closing shell keyword.

Final targeted repair result: `2 passed in 0.65s`.

```bash
uv run --python 3.12 --extra dev pytest tests/unit/docs/test_public_docs_status.py -q
```

Result: `46 passed in 1.35s`.

```bash
uv run --python 3.12 --extra dev ruff check tests/unit/docs/test_public_docs_status.py
```

Result: `All checks passed!`.

```bash
uv run --python 3.12 --extra dev ruff format --check tests/unit/docs/test_public_docs_status.py
```

Result: `1 file already formatted`.

Full AWF/GitHub validation, full coverage, OpenAPI drift checks, and frontend
validation were intentionally not run in the agent phase; AWF owns those broad
gates after agent completion.

Post-review repair for PR thread `PRRT_kwDOSJAM6s6HHCBV`:

- `docs/QUICKSTART.md` now tells package-lane operators to restore the same
  `AWF_API_TOKEN` used by the running local Core and not generate a replacement
  token during upgrade.
- `docs/UPGRADE.md` now applies the same existing-token requirement to the
  `uv tool`, `pipx`, and virtualenv/pip upgrade snippets.
- `tests/unit/docs/test_public_docs_status.py` now rejects package upgrade docs
  that fall back to `openssl rand -hex 32` for `AWF_API_TOKEN` when `.env` does
  not already persist the token.

```bash
uv run --python 3.12 --extra dev pytest tests/unit/docs/test_public_docs_status.py::test_package_upgrade_docs_restore_service_env_before_start tests/unit/docs/test_public_docs_status.py::test_package_upgrade_env_restore_detects_only_closing_fi_keyword -q
```

Red-phase result after updating the focused assertion: failed because
Quickstart Lane 1 did not require the existing `AWF_API_TOKEN`.

Final focused repair result: `2 passed in 0.66s`.

```bash
uv run --python 3.12 --extra dev ruff check tests/unit/docs/test_public_docs_status.py
```

Result: `All checks passed!`.

Full AWF/GitHub validation, full coverage, OpenAPI drift checks, and frontend
validation were intentionally not run in the agent phase; AWF owns those broad
gates after agent completion.

Post-review repair for PR thread `PRRT_kwDOSJAM6s6HHCfa`:

- `docs/UPGRADE.md` now restores `AWF_API_TOKEN` from `.env` or the current
  shell before the release-installed rollback `awf start`, and requires the
  same local Core token instead of generating a replacement.
- `docs/UPGRADE.md` now restores `AWF_POSTGRES_PASSWORD` before that rollback
  start when `.env` does not already persist it.
- `tests/unit/docs/test_public_docs_status.py` now rejects release-installed
  rollback docs that start AWF before restoring the mandatory service env.

```bash
uv run --python 3.12 --extra dev pytest tests/unit/docs/test_public_docs_status.py::test_upgrade_release_installed_rollback_restores_service_env_before_start -q
```

Red-phase result after adding the focused assertion: failed because the
release-installed rollback block had no `AWF_API_TOKEN` restore guard before
`awf start`.

Final focused repair result: `1 passed in 0.65s`.

```bash
uv run --python 3.12 --extra dev pytest tests/unit/docs/test_public_docs_status.py -q
```

Result: `47 passed in 1.39s`.

```bash
uv run --python 3.12 --extra dev ruff check tests/unit/docs/test_public_docs_status.py
```

Result: `All checks passed!`.

Full AWF/GitHub validation, full coverage, OpenAPI drift checks, and frontend
validation were intentionally not run in the agent phase; AWF owns those broad
gates after agent completion.

Post-review repair for review-level comment `issue:4620140358`:

- `docs/UNINSTALL.md` now includes the guarded Docker Compose stop block before
  the introductory source-checkout metadata refresh command.
- `docs/UNINSTALL.md` now includes the same guarded stop block before the
  source-checkout/global-tool and source-checkout/no-global-install refresh
  commands.
- `tests/unit/docs/test_public_docs_status.py` now rejects uninstall guidance
  that tells source-checkout users to stop Core without providing copy-paste
  stop commands before `awf setup --source-checkout ...`.
- `docs/UPGRADE.md` already contained the release-installed rollback env guards;
  the focused rollback regression still passes.

```bash
uv run --python 3.12 --extra dev pytest tests/unit/docs/test_public_docs_status.py::test_uninstall_source_checkout_refresh_requires_core_stop_guidance -q
```

Red-phase result after adding the focused assertion: failed because
`docs/UNINSTALL.md` had no guarded `docker compose ... stop` block before the
introductory source-checkout metadata refresh command.

Final focused repair result: `1 passed in 0.65s`.

```bash
uv run --python 3.12 --extra dev pytest tests/unit/docs/test_public_docs_status.py::test_upgrade_release_installed_rollback_restores_service_env_before_start -q
```

Result: `1 passed in 0.66s`.

```bash
uv run --python 3.12 --extra dev ruff check tests/unit/docs/test_public_docs_status.py
```

Result: `All checks passed!`.

Full AWF/GitHub validation, full coverage, OpenAPI drift checks, and frontend
validation were intentionally not run in the agent phase; AWF owns those broad
gates after agent completion.

## Gaps

None.
