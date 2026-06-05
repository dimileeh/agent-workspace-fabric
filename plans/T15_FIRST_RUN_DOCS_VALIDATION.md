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
- Complete: Address PR thread `PRRT_kwDOSJAM6s6HJDHL` by pinning README
  no-global source-checkout `setup` and `start` commands with
  `--source-checkout "$PWD"` so stale persisted source-checkout metadata cannot
  override the current checkout.
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
- Complete: Address PR thread `PRRT_kwDOSJAM6s6HH2Fh` by preventing
  source-checkout upgrade and rollback snippets from generating a replacement
  `AWF_API_TOKEN` when `docker/compose/.env` or `.env` already carries the
  running local Core token.
- Complete: Address PR thread `PRRT_kwDOSJAM6s6HIPd1` by making
  source-checkout upgrade and rollback snippets preserve a non-default
  persisted `AWF_POSTGRES_PASSWORD` from `docker/compose/.env` or `.env`, and
  require the running local Core password from the shell when no persisted value
  exists.
- Complete: Address review-level comment `issue:4620140358` by guarding the
  reviewer-cited docs ordering assertions with explicit presence checks, while
  leaving the already-correct source-checkout token restoration docs unchanged.
- Complete: Address review-level comment `issue:4620140358` by anchoring the
  package upgrade restart assertion to the standalone `awf start` command line
  and extending the global source-checkout rollback ordering assertion through
  `awf start --source-checkout "$PWD"`.
- Complete: Address review-level comment `issue:4620140358` follow-up by
  splitting mixed Quickstart and Getting Started URL assertions and requiring
  exact, ordered package-upgrade shell anchors for `AWF_API_TOKEN` exports.
- Complete: Address PR thread `PRRT_kwDOSJAM6s6HJ2C1` by persisting Quickstart
  package-lane first-run `AWF_API_TOKEN` and `AWF_POSTGRES_PASSWORD` values to
  `.env` before `awf setup` / `awf start`, so later fresh-shell upgrades can
  restore the same running local Core service values.
- Complete: Address PR thread `PRRT_kwDOSJAM6s6HJ8Ps` by persisting Quickstart
  source-checkout first-run `AWF_API_TOKEN` and `AWF_POSTGRES_PASSWORD` values
  to `docker/compose/.env` before source-checkout setup/start commands, so later
  fresh-shell upgrade, rollback, and uninstall paths can restore the same
  running local Core service values.
- Complete: Address PR thread `PRRT_kwDOSJAM6s6HKFwX` by persisting Getting
  Started package/virtualenv first-run `AWF_API_TOKEN` and
  `AWF_POSTGRES_PASSWORD` values to `.env`, and persisting source-checkout
  first-run values to `docker/compose/.env`, before setup/start commands.
- Complete: Address PR thread `PRRT_kwDOSJAM6s6HKuFE` by splitting the README
  first-run global-source lane away from release-installed PATH commands and
  passing `--source-checkout "$PWD"` to global-source setup/start.
- Complete: Address review-level comment `issue:4620140358` by checking
  Quickstart optional GitHub token guidance per advertised lane first-run
  section and documenting the flat shell guard assumption in
  `_shell_closing_fi_index`.
- Complete: Address PR thread `PRRT_kwDOSJAM6s6HODkj` by allowing Quickstart
  source-checkout upgrade snippets to export the documented `local-dev-token`
  when the copied `.env.example` leaves `AWF_API_TOKEN=` empty and the upgrade
  shell has no token.
- Complete: Address PR thread `PRRT_kwDOSJAM6s6HV58P` by allowing
  `docs/UPGRADE.md` source-checkout upgrade and rollback snippets to export
  `local-dev-token` when source env files contain an empty `AWF_API_TOKEN=`
  entry, while keeping `AWF_POSTGRES_PASSWORD` restore strict.
- Complete: Address PR thread `PRRT_kwDOSJAM6s6HWemN` by making source-checkout
  upgrade, rollback, and uninstall Core stop snippets prefer checkout-root
  `.env` before legacy `docker/compose/.env`, matching source-checkout
  setup/start env precedence while preserving the legacy fallback.
- Complete: Address PR thread `PRRT_kwDOSJAM6s6HWglA` by making the Quickstart
  package-lane first-run snippet persist `AWF_POSTGRES_PASSWORD` as an escaped
  double-quoted dotenv value, preserving `$`, inline `#`, quotes, and
  backslashes under Compose dotenv parsing.
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

```bash
uv run --python 3.12 --extra dev ruff format --check tests/unit/docs/test_public_docs_status.py
```

Result: `1 file already formatted`.

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

```bash
uv run --python 3.12 --extra dev pytest tests/unit/docs/test_public_docs_status.py -q
```

Result: `50 passed in 1.51s`.

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

```bash
uv run --python 3.12 --extra dev pytest tests/unit/docs/test_public_docs_status.py::test_copy_paste_marked_snippets_are_syntactically_valid -q
```

Additional README snippet syntax result: `1 passed in 0.76s`.

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

Post-review repair for PR thread `PRRT_kwDOSJAM6s6HHpsS`:

- `docs/MCP_SETUP.md` now passes `--source-checkout "$PWD"` to both `awf setup`
  and `awf start` in the contributor source-checkout prerequisite block.
- The adjacent source-checkout note now explains that the explicit flag pins the
  checkout just cloned and refreshes stale persisted source-checkout metadata.
- `tests/unit/docs/test_public_docs_status.py` now rejects MCP prerequisite docs
  that leave the source-checkout setup/start commands bare.

```bash
uv run --python 3.12 --extra dev pytest tests/unit/docs/test_public_docs_status.py::test_mcp_setup_prerequisites_use_runnable_startup_path -q
```

Red-phase result after updating the focused assertion: failed because
`docs/MCP_SETUP.md` still had two bare `awf setup` commands and no pinned
source-checkout setup/start pair.

Final focused repair result: `1 passed in 0.65s`.

```bash
uv run --python 3.12 --extra dev pytest tests/unit/docs/test_public_docs_status.py -q
```

Result: `47 passed in 1.49s`.

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

Post-review repair for PR thread `PRRT_kwDOSJAM6s6HH2Fh`:

- `docs/QUICKSTART.md` source-checkout upgrade snippets now check
  `docker/compose/.env` and `.env` before requiring `AWF_API_TOKEN` from the
  shell, and no longer generate a replacement token during upgrade.
- `docs/UPGRADE.md` now documents the source-checkout env-file token reuse rule
  and applies it to both source-checkout upgrade and rollback snippets.
- `tests/unit/docs/test_public_docs_status.py` now rejects source-checkout
  upgrade and rollback snippets that fall back to
  `openssl rand -hex 32` for `AWF_API_TOKEN`.

```bash
uv run --python 3.12 --extra dev pytest tests/unit/docs/test_public_docs_status.py::test_source_checkout_upgrade_docs_refresh_persisted_metadata tests/unit/docs/test_public_docs_status.py::test_upgrade_no_global_source_checkout_rollback_uses_uv_run tests/unit/docs/test_public_docs_status.py::test_upgrade_global_source_checkout_rollback_refreshes_metadata -q
```

Red-phase result after updating the focused assertions: failed because
`docs/QUICKSTART.md` and `docs/UPGRADE.md` still generated a fallback
`AWF_API_TOKEN` in source-checkout upgrade and rollback snippets.

Final focused repair result: `3 passed in 0.69s`.

```bash
uv run --python 3.12 --extra dev pytest tests/unit/docs/test_public_docs_status.py -q
```

Result: `47 passed in 1.42s`.

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

Post-review repair for review-level comment `issue:4620140358`:

- `tests/unit/docs/test_public_docs_status.py` now asserts the reviewer-cited
  uninstall ordering anchors are present before calling `str.index`, so missing
  anchors fail as clear pytest `AssertionError` output instead of bare
  `ValueError: substring not found`.
- `docs/UPGRADE.md` already requires restoring the running Core
  `AWF_API_TOKEN` for source-checkout upgrade and rollback lanes and the
  focused regression rejects `openssl rand -hex 32` token fallback there, so no
  upgrade-doc rewrite was needed for that part of the comment.

```bash
uv run --python 3.12 --extra dev pytest tests/unit/docs/test_public_docs_status.py::test_quickstart_clears_source_checkout_metadata_before_checkout_deletion tests/unit/docs/test_public_docs_status.py::test_uninstall_no_global_source_checkout_cleanup_uses_uv_run tests/unit/docs/test_public_docs_status.py::test_uninstall_global_source_checkout_refreshes_before_tool_uninstall tests/unit/docs/test_public_docs_status.py::test_source_checkout_upgrade_docs_refresh_persisted_metadata -q
```

Result: `4 passed in 0.73s`.

```bash
uv run --python 3.12 --extra dev ruff check tests/unit/docs/test_public_docs_status.py
```

Result: `All checks passed!`.

Full AWF/GitHub validation, full coverage, OpenAPI drift checks, and frontend
validation were intentionally not run in the agent phase; AWF owns those broad
gates after agent completion.

Post-review repair for PR thread `PRRT_kwDOSJAM6s6HIPd1`:

- `docs/QUICKSTART.md` source-checkout upgrade snippets now read
  `AWF_POSTGRES_PASSWORD` from `docker/compose/.env` first, then `.env`, before
  exporting it for `awf start --source-checkout "$PWD"`.
- `docs/UPGRADE.md` applies the same persisted-password preservation to both
  source-checkout upgrade and rollback snippets, and its source-checkout
  overview now covers both service secrets.
- `tests/unit/docs/test_public_docs_status.py` now rejects the old
  source-checkout `AWF_POSTGRES_PASSWORD="${AWF_POSTGRES_PASSWORD:-awf_dev}"`
  fallback and requires the persisted-password restore block.

```bash
uv run --python 3.12 --extra dev pytest tests/unit/docs/test_public_docs_status.py::test_source_checkout_upgrade_docs_refresh_persisted_metadata tests/unit/docs/test_public_docs_status.py::test_upgrade_no_global_source_checkout_rollback_uses_uv_run tests/unit/docs/test_public_docs_status.py::test_upgrade_global_source_checkout_rollback_refreshes_metadata -q
```

Red-phase result after updating the focused assertions: failed because
`docs/QUICKSTART.md` and `docs/UPGRADE.md` still defaulted source-checkout
upgrade and rollback snippets to `awf_dev`.

Final focused repair result: `3 passed in 0.67s`.

```bash
uv run --python 3.12 --extra dev pytest tests/unit/docs/test_public_docs_status.py -q
```

Result: `47 passed in 1.42s`.

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

Post-review repair for review-level comment `issue:4620140358`:

- `tests/unit/docs/test_public_docs_status.py` now rejects package-upgrade docs
  where only prose, not a standalone command line, mentions `awf start`.
- `_assert_package_upgrade_restores_service_env` now anchors the restart check
  to `\nawf start\n`, matching the stricter rollback assertion style.
- `test_upgrade_global_source_checkout_rollback_refreshes_metadata` now asserts
  the source-checkout rollback order through
  `awf start --source-checkout "$PWD"`, not just through metadata refresh.

```bash
uv run --python 3.12 --extra dev pytest tests/unit/docs/test_public_docs_status.py::test_package_upgrade_env_restore_matches_restart_command_line -q
```

Red-phase result after adding the focused regression: failed because a prose
mention of `awf start` satisfied the package upgrade helper.

```bash
uv run --python 3.12 --extra dev pytest tests/unit/docs/test_public_docs_status.py::test_package_upgrade_env_restore_matches_restart_command_line tests/unit/docs/test_public_docs_status.py::test_package_upgrade_docs_restore_service_env_before_start tests/unit/docs/test_public_docs_status.py::test_upgrade_global_source_checkout_rollback_refreshes_metadata tests/unit/docs/test_public_docs_status.py::test_package_upgrade_env_restore_detects_only_closing_fi_keyword -q
```

Result: `4 passed in 0.69s`.

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

Post-review repair for PR thread `PRRT_kwDOSJAM6s6HIjPO`:

- `docs/UPGRADE.md` now requires the running local Core
  `AWF_POSTGRES_PASSWORD` for `uv tool`, `pipx`, and virtualenv/pip upgrade
  snippets when `.env` does not already persist it.
- `docs/QUICKSTART.md` now applies the same package-lane upgrade guidance, so
  the canonical lane selector does not keep the old `awf_dev` fallback.
- `tests/unit/docs/test_public_docs_status.py` now rejects package upgrade docs
  that default `AWF_POSTGRES_PASSWORD` to `awf_dev` instead of requiring the
  existing password.

```bash
uv run --python 3.12 --extra dev pytest tests/unit/docs/test_public_docs_status.py::test_package_upgrade_docs_restore_service_env_before_start -q
```

Red-phase result after tightening the focused assertion: failed because
Quickstart Lane 1 still defaulted `AWF_POSTGRES_PASSWORD` to `awf_dev`.

```bash
uv run --python 3.12 --extra dev pytest tests/unit/docs/test_public_docs_status.py::test_package_upgrade_docs_restore_service_env_before_start tests/unit/docs/test_public_docs_status.py::test_package_upgrade_env_restore_detects_only_closing_fi_keyword tests/unit/docs/test_public_docs_status.py::test_package_upgrade_env_restore_matches_restart_command_line -q
```

Result: `3 passed in 0.67s`.

```bash
uv run --python 3.12 --extra dev ruff check tests/unit/docs/test_public_docs_status.py
```

Result: `All checks passed!`.

Full AWF/GitHub validation, full coverage, OpenAPI drift checks, and frontend
validation were intentionally not run in the agent phase; AWF owns those broad
gates after agent completion.

Post-review repair for review-level comment `issue:4620140358`:

- `tests/unit/docs/test_public_docs_status.py` now has separate Quickstart and
  Getting Started smoke-default URL tests, so failures identify the edited
  document directly.
- `_assert_package_upgrade_restores_service_env` now resolves exact shell lines
  with lower-bound start offsets for the package upgrade env-restore anchors,
  preventing a prefixed `AWF_API_TOKEN` export from satisfying the guard.

```bash
uv run --python 3.12 --extra dev pytest tests/unit/docs/test_public_docs_status.py::test_package_upgrade_env_restore_rejects_prefixed_api_export_line -q
```

Red-phase result after adding the focused regression: failed because
`export AWF_API_TOKEN_BACKUP` still satisfied the unbounded substring lookup.

```bash
uv run --python 3.12 --extra dev pytest tests/unit/docs/test_public_docs_status.py::test_quickstart_first_run_urls_match_smoke_defaults tests/unit/docs/test_public_docs_status.py::test_getting_started_first_run_urls_match_smoke_defaults tests/unit/docs/test_public_docs_status.py::test_package_upgrade_env_restore_rejects_prefixed_api_export_line tests/unit/docs/test_public_docs_status.py::test_package_upgrade_docs_restore_service_env_before_start -q
```

Result: `4 passed in 0.70s`.

```bash
uv run --python 3.12 --extra dev pytest tests/unit/docs/test_public_docs_status.py::test_package_upgrade_env_restore_detects_only_closing_fi_keyword tests/unit/docs/test_public_docs_status.py::test_package_upgrade_env_restore_matches_restart_command_line tests/unit/docs/test_public_docs_status.py::test_package_upgrade_env_restore_rejects_prefixed_api_export_line -q
```

Result: `3 passed in 0.70s`.

```bash
uv run --python 3.12 --extra dev ruff check tests/unit/docs/test_public_docs_status.py
```

Result: `All checks passed!`.

Full AWF/GitHub validation, full coverage, OpenAPI drift checks, and frontend
validation were intentionally not run in the agent phase; AWF owns those broad
gates after agent completion.

Post-review repair for PR thread `PRRT_kwDOSJAM6s6HI2vB`:

- `docs/UNINSTALL.md` now restores or requires the running local Core
  `AWF_API_TOKEN` and `AWF_POSTGRES_PASSWORD` before all source-checkout
  metadata-refresh Compose stop blocks.
- `docs/QUICKSTART.md` applies the same guard to the matching source-checkout
  uninstall snippets, so source-lane users uninstalling from a fresh shell do
  not hit Compose interpolation failures before Core stops.
- `tests/unit/docs/test_public_docs_status.py` now rejects source-checkout
  uninstall snippets that run the Compose stop fallback before restoring service
  secrets.

```bash
uv run --python 3.12 --extra dev pytest tests/unit/docs/test_public_docs_status.py::test_quickstart_clears_source_checkout_metadata_before_checkout_deletion tests/unit/docs/test_public_docs_status.py::test_uninstall_source_checkout_refresh_requires_core_stop_guidance -q
```

Red-phase result after tightening the focused assertions: failed because the
Quickstart and Uninstall source-checkout snippets had no `AWF_API_TOKEN` restore
guard before the Compose stop fallback.

```bash
uv run --python 3.12 --extra dev pytest tests/unit/docs/test_public_docs_status.py::test_quickstart_clears_source_checkout_metadata_before_checkout_deletion tests/unit/docs/test_public_docs_status.py::test_uninstall_source_checkout_refresh_requires_core_stop_guidance -q
```

Final focused repair result: `2 passed in 0.67s`.

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

Post-review repair for PR thread `PRRT_kwDOSJAM6s6HJDHL`:

- `README.md` now passes `--source-checkout "$PWD"` to the no-global
  source-checkout `setup` and `start` commands.
- `tests/unit/docs/test_public_docs_status.py` now rejects bare no-global
  README `setup` or `start` wrapper lines that omit the explicit source
  checkout.

```bash
uv run --python 3.12 --extra dev pytest tests/unit/docs/test_public_docs_status.py::test_readme_first_run_grammar_reuses_initialized_project_path -q
```

Red-phase result after tightening the focused assertion: failed because
`README.md` still omitted `--source-checkout "$PWD"` from the no-global
`setup` command.

```bash
uv run --python 3.12 --extra dev pytest tests/unit/docs/test_public_docs_status.py::test_readme_first_run_grammar_reuses_initialized_project_path -q
```

Final focused repair result: `1 passed in 0.68s`.

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

Post-review repair for PR thread `PRRT_kwDOSJAM6s6HJDHP`:

- `docs/UPGRADE.md` now requires the existing
  `AWF_POSTGRES_PASSWORD` before release-installed and virtualenv rollback
  `awf start` when `.env` does not already persist it.
- `tests/unit/docs/test_public_docs_status.py` now rejects rollback docs that
  default the password to `awf_dev` instead of requiring the running local Core
  password.

```bash
uv run --python 3.12 --extra dev pytest tests/unit/docs/test_public_docs_status.py::test_upgrade_release_installed_rollback_restores_service_env_before_start -q
```

Red-phase result after tightening the focused assertion: failed because
`docs/UPGRADE.md` still omitted the required `AWF_POSTGRES_PASSWORD` restore
line and defaulted to `awf_dev`.

```bash
uv run --python 3.12 --extra dev pytest tests/unit/docs/test_public_docs_status.py::test_upgrade_release_installed_rollback_restores_service_env_before_start -q
```

Final focused repair result: `1 passed in 0.68s`.

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

Post-review repair for PR thread `PRRT_kwDOSJAM6s6HJVcU`:

- `docs/GETTING_STARTED.md` now scopes the bare `awf setup` / `awf start`
  first-run block to package-manager and virtualenv installs.
- The same section now includes source-checkout startup forms for both the
  global `awf` lane and the no-global `uv run --python 3.12 --extra dev awf`
  lane, with `--source-checkout "$PWD"` on setup/start so fresh checkout assets
  are selected explicitly.
- `tests/unit/docs/test_public_docs_status.py` now rejects Getting Started
  source-checkout setup/start commands that omit the explicit checkout flag, and
  rejects bare no-global setup/start wrappers.

```bash
uv run --python 3.12 --extra dev pytest tests/unit/docs/test_public_docs_status.py::test_getting_started_uses_runnable_startup_path -q
```

Red-phase result after tightening the focused assertion: failed because
`docs/GETTING_STARTED.md` did not include `awf setup --source-checkout "$PWD"`.

```bash
uv run --python 3.12 --extra dev pytest tests/unit/docs/test_public_docs_status.py::test_getting_started_uses_runnable_startup_path -q
```

Final focused repair result: `1 passed in 0.68s`.

```bash
uv run --python 3.12 --extra dev pytest tests/unit/docs/test_public_docs_status.py::test_getting_started_uses_runnable_startup_path tests/unit/docs/test_public_docs_status.py::test_getting_started_mocked_smoke_keeps_github_auth_optional tests/unit/docs/test_public_docs_status.py::test_getting_started_first_run_urls_match_smoke_defaults tests/unit/docs/test_public_docs_status.py::test_awf_commands_mentioned_in_public_docs_exist_in_cli_help_tree -q
```

Focused related result: `4 passed in 0.77s`.

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

Post-review repair for PR thread `PRRT_kwDOSJAM6s6HJjsf`:

- Source-checkout upgrade, rollback, and uninstall snippets in
  `docs/QUICKSTART.md`, `docs/UPGRADE.md`, and `docs/UNINSTALL.md` now restore
  `AWF_API_TOKEN` by reading `docker/compose/.env` first, then root `.env`, and
  exporting the persisted value before stopping, setting up, or starting Core.
- `tests/unit/docs/test_public_docs_status.py` now rejects the previous
  two-file `grep` guard because it allowed root `.env` to satisfy the check
  without exporting a token that `awf start --source-checkout` can see when
  `docker/compose/.env` exists.

```bash
uv run --python 3.12 --extra dev pytest tests/unit/docs/test_public_docs_status.py::test_source_checkout_upgrade_docs_refresh_persisted_metadata tests/unit/docs/test_public_docs_status.py::test_upgrade_no_global_source_checkout_rollback_uses_uv_run tests/unit/docs/test_public_docs_status.py::test_upgrade_global_source_checkout_rollback_refreshes_metadata tests/unit/docs/test_public_docs_status.py::test_quickstart_clears_source_checkout_metadata_before_checkout_deletion tests/unit/docs/test_public_docs_status.py::test_uninstall_source_checkout_refresh_requires_core_stop_guidance tests/unit/docs/test_public_docs_status.py::test_uninstall_no_global_source_checkout_cleanup_uses_uv_run tests/unit/docs/test_public_docs_status.py::test_uninstall_global_source_checkout_refreshes_before_tool_uninstall -q
```

Red-phase result after tightening the focused assertion: failed because the
source-checkout snippets still used the shared
`grep -q '^AWF_API_TOKEN=.' docker/compose/.env .env` guard.

```bash
uv run --python 3.12 --extra dev pytest tests/unit/docs/test_public_docs_status.py::test_source_checkout_upgrade_docs_refresh_persisted_metadata tests/unit/docs/test_public_docs_status.py::test_upgrade_no_global_source_checkout_rollback_uses_uv_run tests/unit/docs/test_public_docs_status.py::test_upgrade_global_source_checkout_rollback_refreshes_metadata tests/unit/docs/test_public_docs_status.py::test_quickstart_clears_source_checkout_metadata_before_checkout_deletion tests/unit/docs/test_public_docs_status.py::test_uninstall_source_checkout_refresh_requires_core_stop_guidance tests/unit/docs/test_public_docs_status.py::test_uninstall_no_global_source_checkout_cleanup_uses_uv_run tests/unit/docs/test_public_docs_status.py::test_uninstall_global_source_checkout_refreshes_before_tool_uninstall -q
```

Final focused repair result: `7 passed in 0.80s`.

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

Post-review repair for PR thread `PRRT_kwDOSJAM6s6HJ2C1`:

- `docs/QUICKSTART.md` Lane 1 now tells package-lane users to keep `.env` in
  the current first-run directory and writes the generated `AWF_API_TOKEN` and
  `AWF_POSTGRES_PASSWORD` there before `awf setup`.
- `tests/unit/docs/test_public_docs_status.py` now rejects Quickstart package
  first-run docs that generate service values without persisting them before
  startup.

```bash
uv run --python 3.12 --extra dev pytest tests/unit/docs/test_public_docs_status.py::test_quickstart_package_first_run_persists_service_env_for_upgrade -q
```

Red-phase result after adding the focused assertion: failed because
`docs/QUICKSTART.md` still omitted package-lane `.env` persistence.

```bash
uv run --python 3.12 --extra dev pytest tests/unit/docs/test_public_docs_status.py::test_quickstart_package_first_run_persists_service_env_for_upgrade tests/unit/docs/test_public_docs_status.py::test_quickstart_keeps_package_manager_alternatives_in_separate_blocks tests/unit/docs/test_public_docs_status.py::test_package_upgrade_docs_restore_service_env_before_start -q
```

Final focused repair result: `3 passed in 0.72s`.

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

Post-review repair for PR thread `PRRT_kwDOSJAM6s6HJ8Ps`:

- `docs/QUICKSTART.md` now writes generated source-checkout first-run
  `AWF_API_TOKEN` and `AWF_POSTGRES_PASSWORD` values to
  `docker/compose/.env` before both source-checkout setup/start snippets.
- `tests/unit/docs/test_public_docs_status.py` now rejects Quickstart
  source-checkout first-run snippets that export service values without
  persisting them into the checkout Compose env file before startup.

```bash
uv run --python 3.12 --extra dev pytest tests/unit/docs/test_public_docs_status.py::test_quickstart_source_checkout_first_run_persists_compose_env_for_upgrade -q
```

Red-phase result after adding the focused assertion: failed because both
source-checkout lanes still omitted `docker/compose/.env` persistence before
startup.

```bash
uv run --python 3.12 --extra dev pytest tests/unit/docs/test_public_docs_status.py::test_quickstart_source_checkout_first_run_persists_compose_env_for_upgrade -q
```

Final focused repair result: `2 passed in 0.76s`.

```bash
uv run --python 3.12 --extra dev pytest tests/unit/docs/test_public_docs_status.py::test_quickstart_is_canonical_and_not_a_stub tests/unit/docs/test_public_docs_status.py::test_quickstart_presents_available_complete_first_run_lanes tests/unit/docs/test_public_docs_status.py::test_quickstart_source_checkout_upgrades_reuse_existing_checkout tests/unit/docs/test_public_docs_status.py::test_quickstart_clears_source_checkout_metadata_before_checkout_deletion -q
```

Related Quickstart result: `4 passed in 0.82s`.

```bash
uv run --python 3.12 --extra dev pytest tests/unit/docs/test_public_docs_status.py::test_copy_paste_marked_snippets_are_syntactically_valid -q
```

Shell syntax result: `1 passed in 0.83s`.

```bash
uv run --python 3.12 --extra dev ruff check tests/unit/docs/test_public_docs_status.py
uv run --python 3.12 --extra dev ruff format --check tests/unit/docs/test_public_docs_status.py
```

Result: `All checks passed!`; `1 file already formatted`.

Full AWF/GitHub validation, full coverage, OpenAPI drift checks, and frontend
validation were intentionally not run in the agent phase; AWF owns those broad
gates after agent completion.

Post-review repair for PR thread `PRRT_kwDOSJAM6s6HKFwX`:

- `docs/GETTING_STARTED.md` now writes generated package/virtualenv first-run
  `AWF_API_TOKEN` and `AWF_POSTGRES_PASSWORD` values to `.env` before
  `awf setup`.
- `docs/GETTING_STARTED.md` now writes generated source-checkout first-run
  service values to `docker/compose/.env` before both source-checkout
  setup/start snippets.
- `tests/unit/docs/test_public_docs_status.py` now rejects Getting Started
  first-run snippets that generate service values without persisting them before
  setup.

```bash
uv run --python 3.12 --extra dev pytest tests/unit/docs/test_public_docs_status.py::test_getting_started_first_run_persists_service_env_for_upgrade -q
```

Red-phase result after adding the focused assertion: failed because Getting
Started still omitted first-run `.env` / `docker/compose/.env` persistence before
setup.

```bash
uv run --python 3.12 --extra dev pytest tests/unit/docs/test_public_docs_status.py::test_getting_started_first_run_persists_service_env_for_upgrade tests/unit/docs/test_public_docs_status.py::test_getting_started_uses_runnable_startup_path tests/unit/docs/test_public_docs_status.py::test_getting_started_mocked_smoke_keeps_github_auth_optional tests/unit/docs/test_public_docs_status.py::test_copy_paste_marked_snippets_are_syntactically_valid -q
```

Final focused repair result: `4 passed in 0.76s`.

```bash
uv run --python 3.12 --extra dev ruff check tests/unit/docs/test_public_docs_status.py
uv run --python 3.12 --extra dev ruff format --check tests/unit/docs/test_public_docs_status.py
```

Result: `All checks passed!`; `1 file already formatted`.

Full AWF/GitHub validation, full coverage, OpenAPI drift checks, and frontend
validation were intentionally not run in the agent phase; AWF owns those broad
gates after agent completion.

Post-review repair for PR thread `PRRT_kwDOSJAM6s6HKFwb`:

- `docs/UNINSTALL.md` now anchors the generic source-checkout refresh snippet,
  the source-checkout/global-tool uninstall snippet, and the
  source-checkout/no-global uninstall snippet with
  `cd /path/to/aira-agent-workspace-fabric` before reading
  `docker/compose/.env` or stopping Core through
  `docker/compose/local-service.yml`.
- `tests/unit/docs/test_public_docs_status.py` now rejects source-checkout
  uninstall snippets that use checkout-relative Compose paths before entering
  the checkout.

```bash
uv run --python 3.12 --extra dev pytest tests/unit/docs/test_public_docs_status.py::test_uninstall_source_checkout_refresh_requires_core_stop_guidance -q
```

Red-phase result after adding the focused assertion: failed because
`docs/UNINSTALL.md` omitted `cd /path/to/aira-agent-workspace-fabric` before the
source-checkout env restore and Core stop snippets.

```bash
uv run --python 3.12 --extra dev pytest tests/unit/docs/test_public_docs_status.py::test_uninstall_source_checkout_refresh_requires_core_stop_guidance -q
```

Final focused repair result: `1 passed in 0.65s`.

```bash
uv run --python 3.12 --extra dev pytest tests/unit/docs/test_public_docs_status.py::test_copy_paste_marked_snippets_are_syntactically_valid -q
```

Shell syntax result: `1 passed in 0.76s`.

```bash
uv run --python 3.12 --extra dev ruff check tests/unit/docs/test_public_docs_status.py
uv run --python 3.12 --extra dev ruff format --check tests/unit/docs/test_public_docs_status.py
```

Result: `All checks passed!`; `1 file already formatted`.

Full AWF/GitHub validation, full coverage, OpenAPI drift checks, and frontend
validation were intentionally not run in the agent phase; AWF owns those broad
gates after agent completion.

Post-review repair for PR thread `PRRT_kwDOSJAM6s6HKQiy`:

- `docs/QUICKSTART.md` now derives `AWF_DATABASE_URL` from the same
  `AWF_POSTGRES_PASSWORD` used by Compose in all three first-run lanes.
- `docs/QUICKSTART.md` now persists the matching `AWF_POSTGRES_HOST_PORT` and
  derived `AWF_DATABASE_URL` to `.env` for the package lane and
  `docker/compose/.env` for both source-checkout lanes before `awf setup`.
- `tests/unit/docs/test_public_docs_status.py` now rejects Quickstart
  first-run snippets that persist `AWF_POSTGRES_PASSWORD` without the matching
  host-side database URL.

```bash
uv run --python 3.12 --extra dev pytest tests/unit/docs/test_public_docs_status.py::test_quickstart_package_first_run_persists_service_env_for_upgrade tests/unit/docs/test_public_docs_status.py::test_quickstart_source_checkout_first_run_persists_compose_env_for_upgrade -q
```

Red-phase result after adding the focused assertion: failed because all three
Quickstart first-run lanes omitted `AWF_POSTGRES_HOST_PORT` and the matching
derived `AWF_DATABASE_URL` before setup.

```bash
uv run --python 3.12 --extra dev pytest tests/unit/docs/test_public_docs_status.py::test_quickstart_package_first_run_persists_service_env_for_upgrade tests/unit/docs/test_public_docs_status.py::test_quickstart_source_checkout_first_run_persists_compose_env_for_upgrade tests/unit/docs/test_public_docs_status.py::test_copy_paste_marked_snippets_are_syntactically_valid -q
```

Final focused repair result: `4 passed in 0.84s`.

```bash
uv run --python 3.12 --extra dev ruff check tests/unit/docs/test_public_docs_status.py
uv run --python 3.12 --extra dev ruff format --check tests/unit/docs/test_public_docs_status.py
```

Result: `All checks passed!`; `1 file already formatted`.

Full AWF/GitHub validation, full coverage, OpenAPI drift checks, and frontend
validation were intentionally not run in the agent phase; AWF owns those broad
gates after agent completion.

Post-review repair for PR thread `PRRT_kwDOSJAM6s6HKZLt`:

- `docs/QUICKSTART.md` now persists the package-lane managed AWF service keys
  through a temporary file and replaces only previous values for
  `AWF_API_TOKEN`, `AWF_POSTGRES_PASSWORD`, `AWF_POSTGRES_HOST_PORT`, and
  `AWF_DATABASE_URL`.
- Existing non-AWF `.env` entries are copied forward before the temporary file
  replaces `.env`, so provider tokens, custom AWF settings, and application
  config are not truncated by the first-run block.
- `tests/unit/docs/test_public_docs_status.py` now rejects the unsafe direct
  `} > .env` target and requires the temporary-file preservation path.

```bash
uv run --python 3.12 --extra dev pytest tests/unit/docs/test_public_docs_status.py::test_quickstart_package_first_run_persists_service_env_for_upgrade -q
```

Red-phase result after adding the focused assertion: failed because the
Quickstart package-lane first-run block still wrote directly to `.env` and did
not create `awf_env_tmp`.

```bash
uv run --python 3.12 --extra dev pytest tests/unit/docs/test_public_docs_status.py::test_quickstart_package_first_run_persists_service_env_for_upgrade -q
```

Final focused repair result: `1 passed in 0.67s`.

```bash
uv run --python 3.12 --extra dev ruff check tests/unit/docs/test_public_docs_status.py
uv run --python 3.12 --extra dev ruff format --check tests/unit/docs/test_public_docs_status.py
```

Result: `All checks passed!`; `1 file already formatted`.

Focused shell repro: executed the Quickstart temp-file persistence block in a
temporary directory seeded with `PROVIDER_TOKEN=keep`, `AWF_API_TOKEN=old`, and
`APP_CONFIG=value`. Result: the repro passed; `.env` contained
`AWF_API_TOKEN=new-token`, no longer contained `AWF_API_TOKEN=old`, and retained
both non-AWF entries.

Full AWF/GitHub validation, full coverage, OpenAPI drift checks, and frontend
validation were intentionally not run in the agent phase; AWF owns those broad
gates after agent completion.

Post-review repair for PR thread `PRRT_kwDOSJAM6s6HKggm`:

- `docs/QUICKSTART.md` now uses POSIX/BSD-compatible `sed -e` delete
  expressions to preserve existing package-lane `.env` entries while replacing
  only the managed AWF service keys.
- `tests/unit/docs/test_public_docs_status.py` now rejects the GNU-only basic
  `sed` `\|` alternation and requires the portable multi-`-e` form.

```bash
uv run --python 3.12 --extra dev pytest tests/unit/docs/test_public_docs_status.py::test_quickstart_package_first_run_persists_service_env_for_upgrade -q
```

Red-phase result after updating the focused assertion: failed because
Quickstart still used the GNU-only `sed` `\|` alternation.

```bash
uv run --python 3.12 --extra dev pytest tests/unit/docs/test_public_docs_status.py::test_quickstart_package_first_run_persists_service_env_for_upgrade tests/unit/docs/test_public_docs_status.py::test_copy_paste_marked_snippets_are_syntactically_valid -q
```

Final focused repair result: `2 passed in 0.76s`.

```bash
uv run --python 3.12 --extra dev ruff check tests/unit/docs/test_public_docs_status.py
uv run --python 3.12 --extra dev ruff format --check tests/unit/docs/test_public_docs_status.py
```

Result: `All checks passed!`; `1 file already formatted`.

Full AWF/GitHub validation, full coverage, OpenAPI drift checks, and frontend
validation were intentionally not run in the agent phase; AWF owns those broad
gates after agent completion.

Post-review repair for PR thread `PRRT_kwDOSJAM6s6HKiIB`:

- `docs/GETTING_STARTED.md` now derives `AWF_DATABASE_URL` from the same
  `AWF_POSTGRES_PASSWORD` used by Compose in all three recommended first-run
  snippets.
- `docs/GETTING_STARTED.md` now persists the matching `AWF_POSTGRES_HOST_PORT`
  and derived `AWF_DATABASE_URL` to `.env` for package/virtualenv installs and
  `docker/compose/.env` for both source-checkout snippets before setup/start.
- `tests/unit/docs/test_public_docs_status.py` now rejects Getting Started
  first-run snippets that persist `AWF_POSTGRES_PASSWORD` without the matching
  host-side database URL.

```bash
uv run --python 3.12 --extra dev pytest tests/unit/docs/test_public_docs_status.py::test_getting_started_first_run_persists_service_env_for_upgrade -q
```

Red-phase result after adding the focused assertion: failed because the
Getting Started package/virtualenv first-run block omitted
`AWF_POSTGRES_HOST_PORT` and the matching derived `AWF_DATABASE_URL` before
setup.

```bash
uv run --python 3.12 --extra dev pytest tests/unit/docs/test_public_docs_status.py::test_getting_started_first_run_persists_service_env_for_upgrade tests/unit/docs/test_public_docs_status.py::test_copy_paste_marked_snippets_are_syntactically_valid -q
```

Final focused repair result: `2 passed in 0.75s`.

```bash
uv run --python 3.12 --extra dev ruff check tests/unit/docs/test_public_docs_status.py
uv run --python 3.12 --extra dev ruff format --check tests/unit/docs/test_public_docs_status.py
```

Result: `All checks passed!`; `1 file already formatted`.

Full AWF/GitHub validation, full coverage, OpenAPI drift checks, and frontend
validation were intentionally not run in the agent phase; AWF owns those broad
gates after agent completion.

Post-review repair for PR thread `PRRT_kwDOSJAM6s6HKuFA`:

- `docs/GETTING_STARTED.md` now writes the package-manager / virtualenv
  first-run `.env` values through a temporary file and replaces only
  `AWF_API_TOKEN`, `AWF_POSTGRES_PASSWORD`, `AWF_POSTGRES_HOST_PORT`, and
  `AWF_DATABASE_URL`.
- Existing unrelated `.env` entries are copied forward with portable `sed -e`
  delete expressions before `mv "$awf_env_tmp" .env`, so provider tokens,
  custom AWF settings, and application config are not truncated.
- `tests/unit/docs/test_public_docs_status.py` now rejects the unsafe direct
  `} > .env` target in the Getting Started package/virtualenv block and
  requires the temp-file preservation path.

```bash
uv run --python 3.12 --extra dev pytest tests/unit/docs/test_public_docs_status.py::test_getting_started_first_run_persists_service_env_for_upgrade -q
```

Red-phase result after adding the focused assertion: failed because the
Getting Started package/virtualenv block did not create `awf_env_tmp` and still
wrote directly to `.env`.

```bash
uv run --python 3.12 --extra dev pytest tests/unit/docs/test_public_docs_status.py::test_getting_started_first_run_persists_service_env_for_upgrade tests/unit/docs/test_public_docs_status.py::test_copy_paste_marked_snippets_are_syntactically_valid -q
```

Final focused repair result: `2 passed in 0.76s`.

```bash
uv run --python 3.12 --extra dev ruff check tests/unit/docs/test_public_docs_status.py
uv run --python 3.12 --extra dev ruff format --check tests/unit/docs/test_public_docs_status.py
```

Result: `All checks passed!`; `1 file already formatted`.

Focused shell repro: executed the Getting Started package/virtualenv temp-file
persistence block in a temporary directory seeded with `PROVIDER_TOKEN=keep`,
`AWF_API_TOKEN=old`, and `APP_CONFIG=value`. Result: the repro passed; `.env`
contained the new AWF service values, no longer contained `AWF_API_TOKEN=old`,
and retained both non-AWF entries.

Full AWF/GitHub validation, full coverage, OpenAPI drift checks, and frontend
validation were intentionally not run in the agent phase; AWF owns those broad
gates after agent completion.

Post-review repair for PR thread `PRRT_kwDOSJAM6s6HKuFE`:

- `README.md` now scopes the bare `awf setup` / `awf start` first-run block to
  package-manager and virtualenv lanes.
- `README.md` now gives the source checkout with global tool install lane its
  own startup block with `awf setup --source-checkout "$PWD"` and
  `awf start --source-checkout "$PWD"`, so fresh global source installs use the
  checkout's assets before any persisted `source_checkout` metadata exists.
- `tests/unit/docs/test_public_docs_status.py` now rejects bare README
  setup/start commands inside the global source checkout lane.

```bash
uv run --python 3.12 --extra dev pytest tests/unit/docs/test_public_docs_status.py::test_readme_first_run_grammar_reuses_initialized_project_path -q
```

Red-phase result after updating the focused assertion: failed because the
README still used the shared PATH-lane wording and did not split out the global
source checkout startup block.

```bash
uv run --python 3.12 --extra dev pytest tests/unit/docs/test_public_docs_status.py::test_readme_first_run_grammar_reuses_initialized_project_path -q
```

Final focused repair result: `1 passed in 0.68s`.

```bash
uv run --python 3.12 --extra dev ruff check tests/unit/docs/test_public_docs_status.py
```

Result: `All checks passed!`.

Full AWF/GitHub validation, full coverage, OpenAPI drift checks, and frontend
validation were intentionally not run in the agent phase; AWF owns those broad
gates after agent completion.

Post-review repair for PR thread `PRRT_kwDOSJAM6s6HK9Wu`:

- `docs/QUICKSTART.md` now strips old AWF-managed package-lane `.env` entries
  written as either bare `KEY=...` lines or `export KEY=...` lines, including
  leading whitespace and whitespace before `=`, before appending preserved
  unrelated entries after the newly printed service values.
- `tests/unit/docs/test_public_docs_status.py` now executes the sed expressions
  parsed from the Quickstart first-run block against exported and
  whitespace-padded AWF-managed entries, while preserving unrelated keys.

```bash
uv run --python 3.12 --extra dev pytest tests/unit/docs/test_public_docs_status.py::test_quickstart_package_first_run_strips_exported_awf_env_entries tests/unit/docs/test_public_docs_status.py::test_quickstart_package_first_run_persists_service_env_for_upgrade -q
```

Red-phase result after adding the focused regression: failed because the
Quickstart sed expressions left `export AWF_API_TOKEN=old-token`, exported
password/database URL entries, and a whitespace-padded host-port entry in the
preserved output.

```bash
uv run --python 3.12 --extra dev pytest tests/unit/docs/test_public_docs_status.py::test_quickstart_package_first_run_strips_exported_awf_env_entries tests/unit/docs/test_public_docs_status.py::test_quickstart_package_first_run_persists_service_env_for_upgrade tests/unit/docs/test_public_docs_status.py::test_copy_paste_marked_snippets_are_syntactically_valid -q
```

Final focused repair result: `3 passed in 0.79s`.

```bash
uv run --python 3.12 --extra dev ruff check tests/unit/docs/test_public_docs_status.py
uv run --python 3.12 --extra dev ruff format --check tests/unit/docs/test_public_docs_status.py
```

Result: `All checks passed!`; `1 file already formatted`.

Full AWF/GitHub validation, full coverage, OpenAPI drift checks, and frontend
validation were intentionally not run in the agent phase; AWF owns those broad
gates after agent completion.

Post-review repair for PR thread `PRRT_kwDOSJAM6s6HLHOk`:

- `docs/GETTING_STARTED.md` now strips old package/virtualenv first-run
  AWF-managed `.env` entries written as either bare `KEY=...` lines or
  `export KEY=...` lines, including leading whitespace and whitespace before
  `=`, before appending preserved unrelated entries after the newly printed
  service values.
- `tests/unit/docs/test_public_docs_status.py` now executes the Getting Started
  package/virtualenv sed expressions against exported and whitespace-padded
  AWF-managed entries, while preserving unrelated keys.

```bash
uv run --python 3.12 --extra dev pytest tests/unit/docs/test_public_docs_status.py::test_getting_started_package_first_run_strips_exported_awf_env_entries tests/unit/docs/test_public_docs_status.py::test_getting_started_first_run_persists_service_env_for_upgrade -q
```

Red-phase result after adding the focused regression: failed because the
Getting Started sed expressions left `export AWF_API_TOKEN=old-token`, exported
password/database URL entries, and a whitespace-padded host-port entry in the
preserved output.

```bash
uv run --python 3.12 --extra dev pytest tests/unit/docs/test_public_docs_status.py::test_getting_started_package_first_run_strips_exported_awf_env_entries tests/unit/docs/test_public_docs_status.py::test_getting_started_first_run_persists_service_env_for_upgrade tests/unit/docs/test_public_docs_status.py::test_copy_paste_marked_snippets_are_syntactically_valid -q
```

Final focused repair result: `3 passed in 0.79s`.

```bash
uv run --python 3.12 --extra dev ruff check tests/unit/docs/test_public_docs_status.py
uv run --python 3.12 --extra dev ruff format --check tests/unit/docs/test_public_docs_status.py
```

Result: `All checks passed!`; `1 file already formatted`.

Full AWF/GitHub validation, full coverage, OpenAPI drift checks, and frontend
validation were intentionally not run in the agent phase; AWF owns those broad
gates after agent completion.

Post-review repair for PR thread `PRRT_kwDOSJAM6s6HLWjk`:

- `docs/UPGRADE.md` source-checkout upgrade snippets now restore the persisted
  service env and stop the currently running Core Compose stack before
  `git pull` and before the lane-specific reinstall/sync command.
- `docs/QUICKSTART.md` applies the same source-checkout upgrade ordering, so
  the first-run upgrade lanes do not parse a newly pulled Compose file before
  stopping the old Core stack.
- `tests/unit/docs/test_public_docs_status.py` now rejects source-checkout
  upgrade snippets that pull source changes before the guarded Compose stop.

```bash
uv run --python 3.12 --extra dev pytest tests/unit/docs/test_public_docs_status.py::test_source_checkout_upgrade_docs_refresh_persisted_metadata -q
```

Red-phase result after tightening the focused regression: failed because the
source-checkout upgrade snippets still placed `git pull` before the guarded
Compose stop block.

```bash
uv run --python 3.12 --extra dev pytest tests/unit/docs/test_public_docs_status.py::test_source_checkout_upgrade_docs_refresh_persisted_metadata -q
```

Final focused repair result: `1 passed in 0.67s`.

```bash
uv run --python 3.12 --extra dev pytest tests/unit/docs/test_public_docs_status.py::test_copy_paste_marked_snippets_are_syntactically_valid -q
uv run --python 3.12 --extra dev ruff check tests/unit/docs/test_public_docs_status.py
```

Result: `1 passed in 0.79s`; `All checks passed!`.

Full AWF/GitHub validation, full coverage, OpenAPI drift checks, and frontend
validation were intentionally not run in the agent phase; AWF owns those broad
gates after agent completion.

Post-review repair for PR thread `PRRT_kwDOSJAM6s6HLiZ5`:

- `docs/UPGRADE.md` upgrade and rollback snippets now recognize persisted
  `AWF_API_TOKEN` and `AWF_POSTGRES_PASSWORD` entries written with optional
  leading whitespace and optional `export`, matching dotenv reader behavior.
- `docs/QUICKSTART.md` applies the same package-upgrade guard pattern because
  the focused package upgrade docs assertion covers that matching first-run
  upgrade snippet.
- Source-checkout lifecycle snippets in both docs now use the same
  optional-export/whitespace pattern when reading persisted service secrets from
  `docker/compose/.env` or `.env`.
- `tests/unit/docs/test_public_docs_status.py` now runs the documented package
  guard against an exported `.env` fixture and centralizes the expected package
  guard/source-checkout read lines.

```bash
uv run --python 3.12 --extra dev pytest tests/unit/docs/test_public_docs_status.py::test_package_upgrade_env_restore_accepts_export_prefixed_dotenv_entries tests/unit/docs/test_public_docs_status.py::test_package_upgrade_docs_restore_service_env_before_start tests/unit/docs/test_public_docs_status.py::test_upgrade_release_installed_rollback_restores_service_env_before_start -q
```

Red-phase result after adding the focused regression: failed because the uv tool
upgrade guard still used `grep -q '^AWF_API_TOKEN=.'` and did not match
`export AWF_API_TOKEN=token-from-dotenv`.

```bash
uv run --python 3.12 --extra dev pytest tests/unit/docs/test_public_docs_status.py::test_package_upgrade_env_restore_accepts_export_prefixed_dotenv_entries tests/unit/docs/test_public_docs_status.py::test_package_upgrade_docs_restore_service_env_before_start tests/unit/docs/test_public_docs_status.py::test_upgrade_release_installed_rollback_restores_service_env_before_start tests/unit/docs/test_public_docs_status.py::test_source_checkout_upgrade_docs_refresh_persisted_metadata tests/unit/docs/test_public_docs_status.py::test_upgrade_global_source_checkout_rollback_refreshes_metadata tests/unit/docs/test_public_docs_status.py::test_upgrade_no_global_source_checkout_rollback_uses_uv_run tests/unit/docs/test_public_docs_status.py::test_copy_paste_marked_snippets_are_syntactically_valid -q
```

Final focused repair result: `7 passed in 0.88s`.

```bash
uv run --python 3.12 --extra dev ruff check tests/unit/docs/test_public_docs_status.py
uv run --python 3.12 --extra dev ruff format --check tests/unit/docs/test_public_docs_status.py
```

Result: `All checks passed!`; `1 file already formatted`.

Full AWF/GitHub validation, full coverage, OpenAPI drift checks, and frontend
validation were intentionally not run in the agent phase; AWF owns those broad
gates after agent completion.

Post-review repair for PR thread `PRRT_kwDOSJAM6s6HMa3J`:

- `docs/QUICKSTART.md` source-checkout first-run snippets now write
  `docker/compose/.env` through a temporary file, replacing only
  `AWF_API_TOKEN`, `AWF_POSTGRES_PASSWORD`, `AWF_POSTGRES_HOST_PORT`, and
  `AWF_DATABASE_URL`.
- Existing unrelated Compose env entries such as `AWF_GITHUB_TOKEN`, custom
  host ports, host work directories, provider credentials, and backup keys are
  preserved before `awf setup --source-checkout` / `awf start --source-checkout`.
- `tests/unit/docs/test_public_docs_status.py` now rejects direct
  `} > docker/compose/.env` truncation in the Quickstart source-checkout
  first-run lanes and exercises the documented `sed` expressions against a
  fixture with exported and whitespace-padded AWF service entries.

```bash
uv run --python 3.12 --extra dev pytest tests/unit/docs/test_public_docs_status.py::test_quickstart_source_checkout_first_run_persists_compose_env_for_upgrade tests/unit/docs/test_public_docs_status.py::test_quickstart_source_checkout_first_run_strips_exported_awf_compose_env_entries -q
```

Red-phase result after tightening the focused regression: failed with four
expected failures because both Quickstart source-checkout first-run snippets
still omitted `awf_env_tmp="$(mktemp)"`, used no preservation `sed` block, and
redirected directly to `docker/compose/.env`.

```bash
uv run --python 3.12 --extra dev pytest tests/unit/docs/test_public_docs_status.py::test_quickstart_source_checkout_first_run_persists_compose_env_for_upgrade tests/unit/docs/test_public_docs_status.py::test_quickstart_source_checkout_first_run_strips_exported_awf_compose_env_entries -q
```

Final focused repair result: `4 passed in 0.72s`.

```bash
uv run --python 3.12 --extra dev pytest tests/unit/docs/test_public_docs_status.py::test_copy_paste_marked_snippets_are_syntactically_valid -q
uv run --python 3.12 --extra dev ruff check tests/unit/docs/test_public_docs_status.py
uv run --python 3.12 --extra dev ruff format --check tests/unit/docs/test_public_docs_status.py
```

Result: `1 passed in 0.77s`; `All checks passed!`;
`1 file already formatted`.

Full AWF/GitHub validation, full coverage, OpenAPI drift checks, and frontend
validation were intentionally not run in the agent phase; AWF owns those broad
gates after agent completion.

Post-review repair for PR thread `PRRT_kwDOSJAM6s6HMj0f`:

- `docs/QUICKSTART.md` source-checkout first-run snippets now select an existing
  `docker/compose/.env` first, then checkout-root `.env` as the fallback input,
  before creating the Compose env file.
- The snippets still write `docker/compose/.env` through a temporary file and
  still replace only `AWF_API_TOKEN`, `AWF_POSTGRES_PASSWORD`,
  `AWF_POSTGRES_HOST_PORT`, and `AWF_DATABASE_URL`.
- `tests/unit/docs/test_public_docs_status.py` now rejects source-checkout
  Quickstart snippets that omit the checkout-root `.env` fallback and exercises
  the documented cleanup expressions against a root `.env` fixture.

```bash
uv run --python 3.12 --extra dev pytest tests/unit/docs/test_public_docs_status.py::test_quickstart_source_checkout_first_run_persists_compose_env_for_upgrade tests/unit/docs/test_public_docs_status.py::test_quickstart_source_checkout_first_run_uses_root_env_fallback -q
```

Red-phase result after adding the focused regression: failed with four expected
failures because both Quickstart source-checkout first-run snippets only read
`docker/compose/.env` and had no checkout-root `.env` fallback.

```bash
uv run --python 3.12 --extra dev pytest tests/unit/docs/test_public_docs_status.py::test_quickstart_source_checkout_first_run_persists_compose_env_for_upgrade tests/unit/docs/test_public_docs_status.py::test_quickstart_source_checkout_first_run_uses_root_env_fallback tests/unit/docs/test_public_docs_status.py::test_quickstart_source_checkout_first_run_strips_exported_awf_compose_env_entries -q
```

Final focused repair result: `6 passed in 0.75s`.

```bash
uv run --python 3.12 --extra dev pytest tests/unit/docs/test_public_docs_status.py::test_copy_paste_marked_snippets_are_syntactically_valid -q
uv run --python 3.12 --extra dev ruff check tests/unit/docs/test_public_docs_status.py
uv run --python 3.12 --extra dev ruff format --check tests/unit/docs/test_public_docs_status.py
```

Result: `1 passed in 0.83s`; `All checks passed!`;
`1 file already formatted`.

Full AWF/GitHub validation, full coverage, OpenAPI drift checks, and frontend
validation were intentionally not run in the agent phase; AWF owns those broad
gates after agent completion.

Post-review repair for PR thread `PRRT_kwDOSJAM6s6HM6pL`:

- `docs/GETTING_STARTED.md` source-checkout first-run snippets now write
  `docker/compose/.env` through a temporary file, replacing only
  `AWF_API_TOKEN`, `AWF_POSTGRES_PASSWORD`, `AWF_POSTGRES_HOST_PORT`, and
  `AWF_DATABASE_URL`.
- Existing unrelated Compose env entries such as provider tokens, custom ports,
  host work directories, and other checkout-specific settings are preserved
  before `awf setup --source-checkout` / `awf start --source-checkout`.
- The snippets select an existing `docker/compose/.env` first, then
  checkout-root `.env` as the fallback input, matching the Quickstart source
  lane.
- `tests/unit/docs/test_public_docs_status.py` now rejects direct
  `} > docker/compose/.env` truncation in the Getting Started source-checkout
  first-run lanes.

```bash
uv run --python 3.12 --extra dev pytest tests/unit/docs/test_public_docs_status.py::test_getting_started_first_run_persists_service_env_for_upgrade -q
```

Red-phase result after tightening the focused regression: failed with the
expected assertion because the source-checkout/global executable snippet omitted
`awf_env_tmp="$(mktemp)"` and still redirected directly to
`docker/compose/.env`.

```bash
uv run --python 3.12 --extra dev pytest tests/unit/docs/test_public_docs_status.py::test_getting_started_first_run_persists_service_env_for_upgrade tests/unit/docs/test_public_docs_status.py::test_copy_paste_marked_snippets_are_syntactically_valid -q
uv run --python 3.12 --extra dev ruff check tests/unit/docs/test_public_docs_status.py
uv run --python 3.12 --extra dev ruff format --check tests/unit/docs/test_public_docs_status.py
```

Final focused repair result: `2 passed in 0.80s`; `All checks passed!`;
`1 file already formatted`.

Full AWF/GitHub validation, full coverage, OpenAPI drift checks, and frontend
validation were intentionally not run in the agent phase; AWF owns those broad
gates after agent completion.

Post-review repair for review `4431599164` / inline comment `3359010583`:

- `docs/GETTING_STARTED.md` Configure Environment source-checkout bootstrap now
  selects an existing `docker/compose/.env` first, then checkout-root `.env`,
  then the example template before creating the Compose env file.
- The snippet now writes `docker/compose/.env` through a temporary file,
  replacing only the regenerated `AWF_API_TOKEN`, `AWF_GITHUB_TOKEN`,
  `AWF_POSTGRES_HOST_PORT`, and `AWF_API_HOST_PORT` entries.
- Existing unrelated Compose env entries such as `AWF_POSTGRES_PASSWORD`,
  `AWF_HOST_WORK_DIR`, provider tokens, and custom settings are preserved
  before `uv run --python 3.12 --extra dev awf setup --source-checkout "$PWD"`.
- `tests/unit/docs/test_public_docs_status.py` now rejects direct
  `} > docker/compose/.env` truncation in the Configure Environment
  source-checkout block and exercises the cleanup expressions against an
  existing Compose env fixture.

```bash
uv run --python 3.12 --extra dev pytest tests/unit/docs/test_public_docs_status.py::test_getting_started_configure_environment_preserves_compose_env -q
```

Red-phase result after adding the focused regression: failed with the expected
assertion because the Configure Environment source-checkout block did not
select an existing env source and still redirected directly to
`docker/compose/.env`.

```bash
uv run --python 3.12 --extra dev pytest tests/unit/docs/test_public_docs_status.py::test_getting_started_configure_environment_preserves_compose_env -q
uv run --python 3.12 --extra dev pytest tests/unit/docs/test_public_docs_status.py::test_getting_started_uses_runnable_startup_path tests/unit/docs/test_public_docs_status.py::test_getting_started_configure_environment_preserves_compose_env tests/unit/docs/test_public_docs_status.py::test_getting_started_cli_host_port_derivation_matches_cli_default -q
uv run --python 3.12 --extra dev pytest tests/unit/docs/test_public_docs_status.py::test_copy_paste_marked_snippets_are_syntactically_valid -q
uv run --python 3.12 --extra dev ruff check tests/unit/docs/test_public_docs_status.py
```

Final focused repair result: `1 passed in 0.67s`; `3 passed in 0.73s`;
`1 passed in 0.75s`; `All checks passed!`.

Full AWF/GitHub validation, full coverage, OpenAPI drift checks, and frontend
validation were intentionally not run in the agent phase; AWF owns those broad
gates after agent completion.

Post-review repair for review-level comment `issue:4620140358`:

- `tests/unit/docs/test_public_docs_status.py` now counts the optional and
  manual GitHub token comments inside each advertised Quickstart lane's
  first-run section instead of across the entire document.
- `_shell_closing_fi_index` now documents that it supports the flat `if`/`fi`
  guards used by the docs snippets and should not be reused for nested guards
  without a depth-aware parser.

```bash
uv run --python 3.12 --extra dev pytest tests/unit/docs/test_public_docs_status.py::test_quickstart_mocked_smoke_keeps_github_auth_optional tests/unit/docs/test_public_docs_status.py::test_package_upgrade_env_restore_detects_only_closing_fi_keyword -q
uv run --python 3.12 --extra dev ruff check tests/unit/docs/test_public_docs_status.py
uv run --python 3.12 --extra dev ruff format --check tests/unit/docs/test_public_docs_status.py
```

Final focused repair result: `2 passed in 0.68s`; `All checks passed!`;
`1 file already formatted`.

Full AWF/GitHub validation, full coverage, OpenAPI drift checks, and frontend
validation were intentionally not run in the agent phase; AWF owns those broad
gates after agent completion.

Post-review repair for PR thread `PRRT_kwDOSJAM6s6HOCUX`:

- `docs/QUICKSTART.md` shared GitHub token refresh prerequisites now list a
  lane-specific restart command instead of only `awf start`.
- Lane 1 keeps the bare package-lane `awf start` restart.
- Lane 2 now shows `awf start --source-checkout "$PWD"` from the source
  checkout.
- Lane 3 now shows
  `uv run --python 3.12 --extra dev awf start --source-checkout "$PWD"` from
  the source checkout, matching the no-global startup lane.
- `tests/unit/docs/test_public_docs_status.py` now rejects shared token refresh
  guidance that omits the Lane 2 source-checkout restart or Lane 3 no-global
  wrapper restart.

```bash
uv run --python 3.12 --extra dev pytest tests/unit/docs/test_public_docs_status.py::test_quickstart_token_refresh_restart_is_lane_aware -q
```

Red-phase result after adding the focused regression: failed with the expected
assertion because the shared prerequisites only contained a bare `awf start`
restart block.

```bash
uv run --python 3.12 --extra dev pytest tests/unit/docs/test_public_docs_status.py::test_quickstart_token_refresh_restart_is_lane_aware -q
uv run --python 3.12 --extra dev pytest tests/unit/docs/test_public_docs_status.py::test_quickstart_token_refresh_restart_is_lane_aware tests/unit/docs/test_public_docs_status.py::test_quickstart_uses_runnable_startup_path tests/unit/docs/test_public_docs_status.py::test_raw_docker_compose_source_path_is_single_command -q
uv run --python 3.12 --extra dev ruff check tests/unit/docs/test_public_docs_status.py
uv run --python 3.12 --extra dev ruff format --check tests/unit/docs/test_public_docs_status.py
```

Final focused repair result: `1 passed in 0.70s`; `3 passed in 0.74s`;
`All checks passed!`; `1 file already formatted`.

Full AWF/GitHub validation, full coverage, OpenAPI drift checks, and frontend
validation were intentionally not run in the agent phase; AWF owns those broad
gates after agent completion.

Post-review repair for PR thread `PRRT_kwDOSJAM6s6HODkj`:

- `docs/QUICKSTART.md` Lane 2 and Lane 3 source-checkout upgrade snippets now
  treat a copied empty `AWF_API_TOKEN=` entry as the documented local Compose
  default token, exporting `local-dev-token` when the shell has no
  `AWF_API_TOKEN`.
- `tests/unit/docs/test_public_docs_status.py` now executes the documented
  Quickstart token-restore fragment against `AWF_API_TOKEN=` and an unset shell
  token for both source-checkout lanes.

```bash
uv run --python 3.12 --extra dev pytest tests/unit/docs/test_public_docs_status.py::test_quickstart_source_checkout_upgrade_accepts_default_api_token -q
```

Red-phase result after adding the focused regression: failed with the expected
`${AWF_API_TOKEN:?restore ...}` shell abort for both source-checkout lanes.

```bash
uv run --python 3.12 --extra dev pytest tests/unit/docs/test_public_docs_status.py::test_quickstart_source_checkout_upgrade_accepts_default_api_token -q
uv run --python 3.12 --extra dev pytest tests/unit/docs/test_public_docs_status.py::test_source_checkout_upgrade_docs_refresh_persisted_metadata tests/unit/docs/test_public_docs_status.py::test_copy_paste_marked_snippets_are_syntactically_valid tests/unit/docs/test_public_docs_status.py::test_source_checkout_env_restore_strips_quoted_dotenv_entries -q
uv run --python 3.12 --extra dev ruff check tests/unit/docs/test_public_docs_status.py
```

Final focused repair result: `2 passed in 0.71s`; `5 passed in 0.87s`;
`All checks passed!`.

Full AWF/GitHub validation, full coverage, OpenAPI drift checks, and frontend
validation were intentionally not run in the agent phase; AWF owns those broad
gates after agent completion.

Post-review repair for PR thread `PRRT_kwDOSJAM6s6HODkm`:

- `docs/UPGRADE.md` now says source-checkout lanes read checkout-root `.env`
  first and legacy `docker/compose/.env` only as a fallback.
- Source-checkout upgrade and rollback snippets in `docs/UPGRADE.md` now
  restore `AWF_API_TOKEN` and `AWF_POSTGRES_PASSWORD` from `.env` before
  falling back to `docker/compose/.env`.
- Source-checkout metadata-refresh snippets in `docs/UNINSTALL.md` use the
  same root-first restore order.
- `tests/unit/docs/test_public_docs_status.py` now rejects legacy-first
  source-checkout restore loops and legacy-first prompt text.

```bash
uv run --python 3.12 --extra dev pytest tests/unit/docs/test_public_docs_status.py::test_source_checkout_upgrade_docs_refresh_persisted_metadata tests/unit/docs/test_public_docs_status.py::test_upgrade_global_source_checkout_rollback_refreshes_metadata tests/unit/docs/test_public_docs_status.py::test_upgrade_no_global_source_checkout_rollback_uses_uv_run tests/unit/docs/test_public_docs_status.py::test_uninstall_source_checkout_refresh_requires_core_stop_guidance -q
```

Red-phase result after tightening the focused regression: failed with the
expected assertions that the source-checkout upgrade, rollback, and uninstall
snippets still preferred legacy `docker/compose/.env` over root `.env`.

```bash
uv run --python 3.12 --extra dev pytest tests/unit/docs/test_public_docs_status.py::test_source_checkout_upgrade_docs_refresh_persisted_metadata tests/unit/docs/test_public_docs_status.py::test_upgrade_global_source_checkout_rollback_refreshes_metadata tests/unit/docs/test_public_docs_status.py::test_upgrade_no_global_source_checkout_rollback_uses_uv_run tests/unit/docs/test_public_docs_status.py::test_uninstall_source_checkout_refresh_requires_core_stop_guidance -q
uv run --python 3.12 --extra dev pytest tests/unit/docs/test_public_docs_status.py::test_source_checkout_env_restore_strips_quoted_dotenv_entries tests/unit/docs/test_public_docs_status.py::test_uninstall_source_checkout_env_restore_accepts_exported_dotenv_entries -q
uv run --python 3.12 --extra dev ruff check tests/unit/docs/test_public_docs_status.py
uv run --python 3.12 --extra dev ruff format --check tests/unit/docs/test_public_docs_status.py
```

Final focused repair result: `4 passed in 0.74s`; `4 passed in 0.84s`;
`All checks passed!`; `1 file already formatted`.

Full AWF/GitHub validation, full coverage, OpenAPI drift checks, and frontend
validation were intentionally not run in the agent phase; AWF owns those broad
gates after agent completion.

Post-review repair for PR thread `PRRT_kwDOSJAM6s6HVgpT`:

- `docs/GETTING_STARTED.md` no longer copies source-checkout `.env.example` in
  the package-manager / virtualenv first-run lane.
- That lane now tells package users to run from the directory where AWF should
  keep package-lane `.env`, then generates `AWF_API_TOKEN`,
  `AWF_POSTGRES_PASSWORD`, `AWF_POSTGRES_HOST_PORT`, and `AWF_DATABASE_URL`
  before `awf setup`.
- Existing unrelated `.env` entries are preserved through the same portable
  temp-file pattern used by Quickstart's package lane.
- `tests/unit/docs/test_public_docs_status.py` now rejects Getting Started
  package/virtualenv first-run snippets that copy `.env.example` and requires
  generated root `.env` persistence before setup.

```bash
uv run --python 3.12 --extra dev pytest tests/unit/docs/test_public_docs_status.py::test_getting_started_first_run_persists_service_env_for_upgrade tests/unit/docs/test_public_docs_status.py::test_getting_started_package_first_run_uses_generated_root_env -q
```

Red-phase result after updating the focused assertions: failed because Getting
Started still copied `.env.example` in the package/virtualenv first-run block.

```bash
uv run --python 3.12 --extra dev pytest tests/unit/docs/test_public_docs_status.py::test_getting_started_first_run_persists_service_env_for_upgrade tests/unit/docs/test_public_docs_status.py::test_getting_started_package_first_run_uses_generated_root_env -q
uv run --python 3.12 --extra dev pytest tests/unit/docs/test_public_docs_status.py::test_getting_started_first_run_persists_service_env_for_upgrade tests/unit/docs/test_public_docs_status.py::test_getting_started_package_first_run_uses_generated_root_env tests/unit/docs/test_public_docs_status.py::test_getting_started_uses_runnable_startup_path tests/unit/docs/test_public_docs_status.py::test_getting_started_mocked_smoke_keeps_github_auth_optional tests/unit/docs/test_public_docs_status.py::test_copy_paste_marked_snippets_are_syntactically_valid -q
uv run --python 3.12 --extra dev ruff check tests/unit/docs/test_public_docs_status.py
uv run --python 3.12 --extra dev ruff format --check tests/unit/docs/test_public_docs_status.py
```

Final focused repair result: `2 passed in 0.68s`; `5 passed in 0.84s`;
`All checks passed!`; `1 file already formatted`.

Full AWF/GitHub validation, full coverage, OpenAPI drift checks, and frontend
validation were intentionally not run in the agent phase; AWF owns those broad
gates after agent completion.

Post-review repair for PR thread `PRRT_kwDOSJAM6s6HV58P`:

- `docs/UPGRADE.md` now documents that source-checkout `AWF_API_TOKEN=` entries
  copied from `.env.example` keep the local `local-dev-token` default.
- Source-checkout upgrade and rollback snippets in `docs/UPGRADE.md` now export
  `local-dev-token` when `.env` or legacy `docker/compose/.env` contains an
  empty `AWF_API_TOKEN=` entry and the shell has no token.
- `AWF_POSTGRES_PASSWORD` restore behavior is unchanged: source-checkout
  snippets still require a persisted non-empty value or an explicit shell value.
- `tests/unit/docs/test_public_docs_status.py` now executes the UPGRADE source
  API-token restore snippets against an empty copied `.env` entry and requires
  the fallback in the shared source restore assertion for upgrade/rollback
  lifecycles only.

```bash
uv run --python 3.12 --extra dev pytest tests/unit/docs/test_public_docs_status.py::test_upgrade_source_checkout_restore_accepts_default_api_token tests/unit/docs/test_public_docs_status.py::test_source_checkout_upgrade_docs_refresh_persisted_metadata tests/unit/docs/test_public_docs_status.py::test_upgrade_global_source_checkout_rollback_refreshes_metadata tests/unit/docs/test_public_docs_status.py::test_upgrade_no_global_source_checkout_rollback_uses_uv_run -q
```

Red-phase result after updating the focused regression: failed with the expected
assertions that UPGRADE source-checkout upgrade and rollback snippets still
required `AWF_API_TOKEN` instead of accepting the empty copied `.env` default.

```bash
uv run --python 3.12 --extra dev pytest tests/unit/docs/test_public_docs_status.py::test_upgrade_source_checkout_restore_accepts_default_api_token tests/unit/docs/test_public_docs_status.py::test_source_checkout_upgrade_docs_refresh_persisted_metadata tests/unit/docs/test_public_docs_status.py::test_upgrade_global_source_checkout_rollback_refreshes_metadata tests/unit/docs/test_public_docs_status.py::test_upgrade_no_global_source_checkout_rollback_uses_uv_run -q
uv run --python 3.12 --extra dev ruff check tests/unit/docs/test_public_docs_status.py
uv run --python 3.12 --extra dev ruff format --check tests/unit/docs/test_public_docs_status.py
```

Final focused repair result: initial regression run failed as expected with
`4 failed`; final focused pytest result was `4 passed in 0.77s`; final
`ruff check` passed; final `ruff format --check` reported
`1 file already formatted`.

Full AWF/GitHub validation, full coverage, OpenAPI drift checks, and frontend
validation were intentionally not run in the agent phase; AWF owns those broad
gates after agent completion.

Post-review repair for PR thread `PRRT_kwDOSJAM6s6HWIlO`:

- `docs/QUICKSTART.md` Lane 1 now URL-encodes `AWF_POSTGRES_PASSWORD` before
  embedding it in the derived `AWF_DATABASE_URL`.
- `docs/GETTING_STARTED.md` applies the same correction to the mirrored
  package-manager / virtualenv first-run snippet.
- Both snippets still persist the raw `AWF_POSTGRES_PASSWORD` separately so
  Compose and later upgrade restore use the original password, while
  `AWF_DATABASE_URL` stores the encoded URL component.
- `tests/unit/docs/test_public_docs_status.py` now rejects the raw-password URL
  interpolation and executes the Quickstart first-run env-persistence snippet
  with `@`, `/`, `:`, and `#` in the custom password.

```bash
uv run --python 3.12 --extra dev pytest tests/unit/docs/test_public_docs_status.py::test_quickstart_package_first_run_url_encodes_custom_postgres_password tests/unit/docs/test_public_docs_status.py::test_quickstart_package_first_run_persists_service_env_for_upgrade tests/unit/docs/test_public_docs_status.py::test_getting_started_first_run_persists_service_env_for_upgrade tests/unit/docs/test_public_docs_status.py::test_getting_started_package_first_run_uses_generated_root_env -q
```

Red-phase result after updating the focused assertions: failed as expected with
`3 failed, 1 passed`; the executable Quickstart snippet persisted
`AWF_DATABASE_URL=postgresql+asyncpg://awf:p@ss/word:with#reserved@localhost:5433/awf`
instead of the encoded password URL.

```bash
uv run --python 3.12 --extra dev pytest tests/unit/docs/test_public_docs_status.py::test_quickstart_package_first_run_url_encodes_custom_postgres_password tests/unit/docs/test_public_docs_status.py::test_quickstart_package_first_run_persists_service_env_for_upgrade tests/unit/docs/test_public_docs_status.py::test_getting_started_first_run_persists_service_env_for_upgrade tests/unit/docs/test_public_docs_status.py::test_getting_started_package_first_run_uses_generated_root_env tests/unit/docs/test_public_docs_status.py::test_copy_paste_marked_snippets_are_syntactically_valid -q
uv run --python 3.12 --extra dev ruff check tests/unit/docs/test_public_docs_status.py
uv run --python 3.12 --extra dev ruff format --check tests/unit/docs/test_public_docs_status.py
```

Final focused repair result: `5 passed in 0.93s`; `ruff check` passed;
`ruff format --check` reported `1 file already formatted`.

Full AWF/GitHub validation, full coverage, OpenAPI drift checks, and frontend
validation were intentionally not run in the agent phase; AWF owns those broad
gates after agent completion.

Post-review repair for PR thread `PRRT_kwDOSJAM6s6HWWni`:

- `docs/UNINSTALL.md` now exports `local-dev-token` when source-checkout
  uninstall metadata-refresh snippets find an empty persisted `AWF_API_TOKEN=`
  entry in `.env` or legacy `docker/compose/.env` and the shell has no token.
- Matching Quickstart inline source-checkout uninstall snippets now apply the
  same fallback for the `.env` copied from `.env.example`.
- `AWF_POSTGRES_PASSWORD` restore behavior remains strict and still requires a
  non-empty persisted value or an explicit shell value.
- `tests/unit/docs/test_public_docs_status.py` now executes the UNINSTALL
  source API-token restore snippets against an empty copied `.env` entry, and
  the shared source-checkout restore assertion requires the default-token
  branch for uninstall metadata refreshes.

```bash
uv run --python 3.12 --extra dev pytest tests/unit/docs/test_public_docs_status.py::test_uninstall_source_checkout_restore_accepts_default_api_token tests/unit/docs/test_public_docs_status.py::test_uninstall_source_checkout_refresh_requires_core_stop_guidance tests/unit/docs/test_public_docs_status.py::test_quickstart_clears_source_checkout_metadata_before_checkout_deletion -q
```

Red-phase result after updating the focused regression: failed as expected with
`3 failed`; the standalone `docs/UNINSTALL.md` restore block still aborted with
`AWF_API_TOKEN: restore the AWF_API_TOKEN...`, and the shared assertions found
the missing default-token branch in UNINSTALL and Quickstart uninstall snippets.

```bash
uv run --python 3.12 --extra dev pytest tests/unit/docs/test_public_docs_status.py::test_uninstall_source_checkout_restore_accepts_default_api_token tests/unit/docs/test_public_docs_status.py::test_uninstall_source_checkout_refresh_requires_core_stop_guidance tests/unit/docs/test_public_docs_status.py::test_quickstart_clears_source_checkout_metadata_before_checkout_deletion -q
uv run --python 3.12 --extra dev ruff check tests/unit/docs/test_public_docs_status.py
uv run --python 3.12 --extra dev ruff format --check tests/unit/docs/test_public_docs_status.py
git diff --check
```

Final focused repair result: `3 passed in 0.80s`; `ruff check` passed;
`ruff format --check` reported `1 file already formatted`; `git diff --check`
reported no whitespace errors.

Full AWF/GitHub validation, full coverage, OpenAPI drift checks, and frontend
validation were intentionally not run in the agent phase; AWF owns those broad
gates after agent completion.

Post-review repair for PR thread `PRRT_kwDOSJAM6s6HWemN`:

- `docs/UPGRADE.md` source-checkout upgrade and rollback snippets now stop Core
  with checkout-root `.env` when it exists, then fall back to legacy
  `docker/compose/.env`, then to a stop command without an env file.
- `docs/UNINSTALL.md` applies the same root-first stop selection to the
  introductory source-checkout metadata refresh snippet and both source-checkout
  uninstall lanes.
- `tests/unit/docs/test_public_docs_status.py` now rejects legacy-first stop
  guards and requires UPGRADE/UNINSTALL source-checkout snippets to preserve
  root-first legacy fallback ordering.

```bash
uv run --python 3.12 --extra dev pytest tests/unit/docs/test_public_docs_status.py::test_source_checkout_upgrade_docs_refresh_persisted_metadata tests/unit/docs/test_public_docs_status.py::test_uninstall_source_checkout_refresh_requires_core_stop_guidance tests/unit/docs/test_public_docs_status.py::test_upgrade_global_source_checkout_rollback_refreshes_metadata tests/unit/docs/test_public_docs_status.py::test_upgrade_no_global_source_checkout_rollback_uses_uv_run -q
```

Red-phase result after updating the focused regression: failed as expected with
`4 failed`; the assertions rejected the legacy-first
`if [ -f docker/compose/.env ]; then` stop guard in UPGRADE and UNINSTALL.

```bash
uv run --python 3.12 --extra dev pytest tests/unit/docs/test_public_docs_status.py::test_source_checkout_upgrade_docs_refresh_persisted_metadata tests/unit/docs/test_public_docs_status.py::test_uninstall_source_checkout_refresh_requires_core_stop_guidance tests/unit/docs/test_public_docs_status.py::test_upgrade_global_source_checkout_rollback_refreshes_metadata tests/unit/docs/test_public_docs_status.py::test_upgrade_no_global_source_checkout_rollback_uses_uv_run -q
uv run --python 3.12 --extra dev pytest tests/unit/docs/test_public_docs_status.py::test_copy_paste_marked_snippets_are_syntactically_valid -q
uv run --python 3.12 --extra dev ruff check tests/unit/docs/test_public_docs_status.py
uv run --python 3.12 --extra dev ruff format --check tests/unit/docs/test_public_docs_status.py
```

Final focused repair result: `4 passed in 0.80s`; snippet syntax check
`1 passed in 0.75s`; `ruff check` passed; `ruff format --check` reported
`1 file already formatted`.

Full AWF/GitHub validation, full coverage, OpenAPI drift checks, and frontend
validation were intentionally not run in the agent phase; AWF owns those broad
gates after agent completion.

Post-review repair for PR thread `PRRT_kwDOSJAM6s6HWglA`:

- `docs/QUICKSTART.md` Lane 1 now derives an escaped double-quoted dotenv copy
  of `AWF_POSTGRES_PASSWORD`, rejects newline-containing passwords that cannot
  be represented in the one-line env snippet, and persists that escaped value
  to `.env`.
- `AWF_DATABASE_URL` still uses the URL-encoded password derived from the
  original shell value.
- `tests/unit/docs/test_public_docs_status.py` now executes the Quickstart
  first-run env-persistence snippet with `$`, inline `#`, quotes, and a
  backslash in the custom password and verifies the resulting `.env` parses
  back to the original password under AWF's Compose env parser.

```bash
uv run --python 3.12 --extra dev pytest tests/unit/docs/test_public_docs_status.py::test_quickstart_package_first_run_persists_service_env_for_upgrade tests/unit/docs/test_public_docs_status.py::test_quickstart_package_first_run_url_encodes_custom_postgres_password -q
```

Red-phase result after updating the focused regression: failed as expected with
`2 failed`; the current Quickstart still lacked
`awf_postgres_password_dotenv` and persisted the raw password line.

```bash
uv run --python 3.12 --extra dev pytest tests/unit/docs/test_public_docs_status.py::test_quickstart_package_first_run_persists_service_env_for_upgrade tests/unit/docs/test_public_docs_status.py::test_quickstart_package_first_run_url_encodes_custom_postgres_password tests/unit/docs/test_public_docs_status.py::test_copy_paste_marked_snippets_are_syntactically_valid -q
uv run --python 3.12 --extra dev ruff check tests/unit/docs/test_public_docs_status.py
uv run --python 3.12 --extra dev ruff format --check tests/unit/docs/test_public_docs_status.py
```

Final focused repair result: `3 passed in 1.02s`; `ruff check` passed;
`ruff format --check` reported `1 file already formatted`.

Full AWF/GitHub validation, full coverage, OpenAPI drift checks, and frontend
validation were intentionally not run in the agent phase; AWF owns those broad
gates after agent completion.

## Gaps

None.
