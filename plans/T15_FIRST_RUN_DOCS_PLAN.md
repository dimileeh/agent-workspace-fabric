# T15 First-Run Docs Plan

## Problem Statement And Scope

Update AWF public first-run documentation so an evaluator can choose one of four
complete lanes and follow that lane without mixing commands from another lane.
The update is scoped to documentation and focused docs tests; no CLI/runtime,
packaging, OpenAPI, frontend, migration, or lockfile changes are planned.

Source contract: `docs/awf-plans/ws_b77253c13d91444db1348fc1.md`.

## Requirements Checklist

- Present the currently available public first-run lanes: `uv tool` / `pipx`,
  source checkout with global tool install, and source checkout with no global
  install.
- Omit the hosted curl installer lane from public first-run docs until the
  installer, manifest, checksums, and distribution artifacts are published and
  verified.
- Explain which lanes are release-installed and which lanes are inspectable.
- For each lane, document setup, start, project init, mocked smoke, upgrade, and
  uninstall.
- Use current first-run grammar: `awf setup`, `awf start`, `awf init <path>` or
  project-local `awf init .`, and
  `awf smoke run --mocked-local --format pretty`.
- Keep local API and console URLs aligned with the default smoke probe targets.
- Remove stale placeholder/no-path bootstrap language from public first-run docs.
- Add `docs/UNINSTALL.md` with lane-specific uninstall guidance that separates
  CLI/tool removal from destructive local state cleanup.
- Update focused docs tests so stale command grammar is rejected.
- Leave broad AWF/GitHub validation to post-agent infrastructure.

## Implementation Steps

Post-review adjustment for PR thread `PRRT_kwDOSJAM6s6G_99y`: until the
hosted installer, manifest, checksums, and distribution artifacts are published
and verified, `docs/QUICKSTART.md` must steer evaluators to the `uv tool` /
`pipx` or source-checkout lanes instead of advertising
`https://aira.pro/install.sh`.

Post-review adjustment for review-level comment `issue:4620140358`: Lane 1
Quickstart prose must not imply the newly created eval directory is already a
checked-out repository, and the focused Getting Started grammar test must report
missing Markdown headings with explicit assertions instead of indexed split
failures. While validating that test, keep its smoke command assertion aligned
with the current `--project <path>` Getting Started guidance.

Post-review adjustment for PR thread `PRRT_kwDOSJAM6s6HAl67`: Upgrade and
rollback smoke commands in `docs/UPGRADE.md` must pass the project path that was
initialized during first run because `awf smoke run` defaults `--project` to the
current working directory.

Post-review adjustment for PR thread `PRRT_kwDOSJAM6s6HAx2p`: Upgrade smoke
commands in `docs/QUICKSTART.md` must pass the same project paths initialized in
their lanes because `awf smoke run` defaults `--project` to the current working
directory.

Post-review adjustment for PR thread `PRRT_kwDOSJAM6s6HBU4r`: mocked first-run
command blocks in `docs/QUICKSTART.md` must not hard-require `gh auth token`;
leave `AWF_GITHUB_TOKEN` explicit but commented as optional for PR creation and
monitoring features.

Post-review adjustment for PR thread `PRRT_kwDOSJAM6s6HBU-B`: Quickstart first-run
URL prose must document the actual default smoke probe targets,
`http://localhost:8000` and `http://localhost:3000`, unless the code defaults
change.

Post-review adjustment for review-level comment `issue:4620140358`: lifecycle
guides must document the virtualenv/pip path that README still advertises, the
focused Markdown section helper must reject H3-or-deeper headings instead of
silently over-capturing, and the hosted-curl omission assertion must fail with a
clear document-specific message.

Post-review adjustment for PR thread `PRRT_kwDOSJAM6s6HB-xB`: source-checkout
uninstall guidance must not tell operators to delete a checkout while
`~/.awf/config.yml` still points `source_checkout` at it. Document how to refresh
the persisted checkout path or remove only the persisted `source_checkout`
metadata before deleting that checkout.

Post-review adjustment for PR thread `PRRT_kwDOSJAM6s6HCN6b`: the self-contained
Quickstart source-checkout/no-global-install uninstall lane must clear or refresh
persisted `source_checkout` metadata before deleting the recorded checkout.

Post-review adjustment for PR thread `PRRT_kwDOSJAM6s6HCmnm`: the Quickstart
source-checkout/global-tool uninstall lane must clear or refresh persisted
`source_checkout` metadata before deleting the recorded checkout because
`awf start` revalidates that metadata when no explicit source checkout is passed.

Post-review adjustment for review-level comment `issue:4620140358`: Getting
Started must not contradict the Quickstart smoke-default `localhost` URLs with a
blanket `127.0.0.1` first-run statement, and the Quickstart optional GitHub
token assertion must calibrate itself from the advertised lane headings instead
of hard-coding the current lane count.

Post-review adjustment for PR thread `PRRT_kwDOSJAM6s6HC6Eb`: source-checkout
upgrade snippets in `docs/QUICKSTART.md` and `docs/UPGRADE.md` must refresh
persisted source-checkout metadata with `awf setup --source-checkout "$PWD"`
after the pull/install or sync step and before the matching
`awf start --source-checkout "$PWD"` command.

Post-review adjustment for PR thread `PRRT_kwDOSJAM6s6HDHwd`: the `uv tool` and
`pipx` alternatives in `docs/QUICKSTART.md` Lane 1 must not be executable in the
same copy-paste bash block; split install, upgrade, and uninstall alternatives
so copying one block cannot run both package managers.

Post-review adjustment for PR thread `PRRT_kwDOSJAM6s6HE3SX`: README first-run
commands must not advertise bare `awf` commands as applying to the source
checkout lane with no global install. Scope the bare command block to lanes that
put `awf` on `PATH`, and show the no-global `uv run --python 3.12 --extra dev
awf ...` wrapper alongside it.

Post-review adjustment for PR thread `PRRT_kwDOSJAM6s6HFJSc`: Getting Started's
mocked first-run sequence must not hard-require `gh auth token`; keep GitHub
token guidance optional there, matching Quickstart, and leave required token
setup in the PR monitoring/provider sections.

Post-review adjustment for PR thread `PRRT_kwDOSJAM6s6HFXeN`: source-checkout
upgrade snippets must stop the local Core Compose stack before refreshing
persisted `source_checkout` metadata with `awf setup --source-checkout "$PWD"`,
because setup readiness blocks while the running API/Postgres services still
hold the default host ports.

Post-review adjustment for PR thread `PRRT_kwDOSJAM6s6HFn6N`: source-checkout
upgrade snippets must not require `docker/compose/.env` when the source-checkout
first-run lane only exported shell values. Guard `--env-file docker/compose/.env`
behind an existence check and provide a fallback stop command that omits it.

Post-review adjustment for PR thread `PRRT_kwDOSJAM6s6HF4n7`: the rollback guide's
global `awf` path must not also cover source-checkout/global-tool rollbacks. That
source lane must stop local Core, refresh persisted source-checkout metadata with
`awf setup --source-checkout "$PWD"`, then start with
`awf start --source-checkout "$PWD"` from the restored checkout.

Post-review adjustment for PR thread `PRRT_kwDOSJAM6s6HGN4c`: package and
virtualenv upgrade snippets must restore the mandatory local service values
before `awf start` when no `.env` already persists them. Preserve an existing
`.env` as the preferred read source so upgrade snippets do not accidentally
override persisted service secrets.

Post-review adjustment for review comment `issue:4620140358`: the package
upgrade env-restore assertion must identify only a shell closing `fi` keyword
anchored on its own line, and `docs/MCP_SETUP.md` prerequisites must provide a
clear `awf service status --format pretty` check after each `awf start` example.

Post-review adjustment for PR thread `PRRT_kwDOSJAM6s6HHCBV`: package and
virtualenv upgrade snippets must not generate a fresh `AWF_API_TOKEN` when the
current `.env` does not already persist one. Require operators to restore the
same API token used for the running local Core before restarting, so upgrades do
not desynchronize CLI/API authentication.

Post-review adjustment for PR thread `PRRT_kwDOSJAM6s6HHCfa`: the
release-installed and virtualenv rollback snippet in `docs/UPGRADE.md` must
restore `AWF_API_TOKEN` and `AWF_POSTGRES_PASSWORD` from `.env` or the current
shell before `awf start`, matching the package upgrade guidance.

Post-review adjustment for review-level comment `issue:4620140358`:
`docs/UNINSTALL.md` must include the guarded Docker Compose stop commands before
each source-checkout metadata refresh command, including the introductory
refresh example and both source-checkout uninstall sections.

Post-review adjustment for PR thread `PRRT_kwDOSJAM6s6HHpsS`:
`docs/MCP_SETUP.md` contributor source-checkout prerequisites must pass
`--source-checkout "$PWD"` to both `awf setup` and `awf start` so a newly cloned
checkout is selected even when stale source-checkout metadata already exists.

Post-review adjustment for PR thread `PRRT_kwDOSJAM6s6HH2Fh`: source-checkout
upgrade and rollback snippets in `docs/QUICKSTART.md` and `docs/UPGRADE.md`
must not generate a replacement `AWF_API_TOKEN` when the shell variable is
unset. Reuse a token already persisted in `docker/compose/.env` or `.env`; only
require operators to export the running local Core token when neither file
contains it.

Post-review adjustment for review-level comment `issue:4620140358`: docs
ordering assertions cited by the reviewer must assert each ordering anchor is
present before calling `str.index`, so future documentation regressions fail
with clear `AssertionError` messages instead of opaque `ValueError` exceptions.
The same comment's source-checkout token-regeneration concern is already
addressed by the current `docs/UPGRADE.md` `AWF_API_TOKEN` guards and
regression assertions, so avoid unrelated docs churn there.

Post-review adjustment for PR thread `PRRT_kwDOSJAM6s6HIPd1`: source-checkout
upgrade and rollback snippets in `docs/QUICKSTART.md` and `docs/UPGRADE.md`
must preserve a non-default persisted `AWF_POSTGRES_PASSWORD` from
`docker/compose/.env` or the checkout root `.env` before `awf start` overlays
the process environment. If neither persisted file carries the password, the
snippets must require the operator to restore the running local Core password in
the shell instead of defaulting to `awf_dev`.

Post-review adjustment for PR thread `PRRT_kwDOSJAM6s6HI2vB`: source-checkout
uninstall metadata-refresh snippets in `docs/UNINSTALL.md` and the matching
Quickstart source lanes must restore or require the running local Core
`AWF_API_TOKEN` and `AWF_POSTGRES_PASSWORD` before the Compose stop fallback.
This prevents fresh-shell source-checkout uninstalls from failing Compose
interpolation before Core stops.

Post-review adjustment for review-level comment `issue:4620140358`: package
upgrade env-restore assertions must match the standalone `awf start` command
line instead of prose substrings, and the global source-checkout rollback
assertion must explicitly prove the documented stop, env-restore, setup, and
start ordering.

Post-review adjustment for PR thread `PRRT_kwDOSJAM6s6HIjPO`: release-installed
and virtualenv package upgrade snippets in `docs/QUICKSTART.md` and
`docs/UPGRADE.md` must require operators to restore the running local Core
`AWF_POSTGRES_PASSWORD` when `.env` does not already persist it. These snippets
must not default the password to `awf_dev`, because an existing Postgres volume
may have been initialized with a non-default password.

Post-review adjustment for review-level comment `issue:4620140358`: split the
mixed Quickstart and Getting Started first-run URL assertions so failures point
at the changed document, and tighten package upgrade env-restore anchor lookup
so the `AWF_API_TOKEN` export match is an exact shell line found after the
preceding guard/require anchors.

Post-review adjustment for PR thread `PRRT_kwDOSJAM6s6HJDHL`: README no-global
source-checkout setup/start commands must pass `--source-checkout "$PWD"` so a
fresh checkout is selected even when stale persisted `source_checkout` metadata
exists.

Post-review adjustment for PR thread `PRRT_kwDOSJAM6s6HJDHP`: the
release-installed and virtualenv rollback snippet in `docs/UPGRADE.md` must
require operators to restore the running local Core `AWF_POSTGRES_PASSWORD`
when `.env` does not already persist it. It must not fall back to `awf_dev`,
because the existing Postgres volume may require a non-default password.

Post-review adjustment for PR thread `PRRT_kwDOSJAM6s6HJVcU`: Getting Started's
recommended first-run sequence must not present a bare `awf setup` / `awf start`
block as copy-pasteable for the no-global source-checkout lane. Scope the bare
block to installs with `awf` on `PATH` and show the no-global
`uv run --python 3.12 --extra dev awf setup --source-checkout "$PWD"` /
`start --source-checkout "$PWD"` startup variant so a fresh checkout is selected
even when persisted source-checkout metadata is stale or absent.

Post-review adjustment for PR thread `PRRT_kwDOSJAM6s6HJjsf`: source-checkout
upgrade, rollback, and uninstall snippets must not let a root `.env`
`AWF_API_TOKEN` satisfy the guard without exporting it. When
`docker/compose/.env` exists, `awf start --source-checkout` reads that compose
env file and only sees root `.env` values if the shell exports them. The docs
should restore `AWF_API_TOKEN` from `docker/compose/.env` first, then root
`.env`, and export whichever persisted value is found before stopping or
starting Core.

Post-review adjustment for PR thread `PRRT_kwDOSJAM6s6HJ2C1`: the Quickstart
package lane must persist the first-run `AWF_API_TOKEN` and
`AWF_POSTGRES_PASSWORD` to `.env` before `awf setup` / `awf start`, so a later
fresh-shell upgrade can restore the same running local Core service values.

Post-review adjustment for PR thread `PRRT_kwDOSJAM6s6HJ8Ps`: the Quickstart
source-checkout lanes must persist the first-run `AWF_API_TOKEN` and
`AWF_POSTGRES_PASSWORD` to `docker/compose/.env` before
`awf setup --source-checkout` / `awf start --source-checkout`, so later
fresh-shell upgrade, rollback, and uninstall paths can restore the same running
local Core service values.

Post-review adjustment for PR thread `PRRT_kwDOSJAM6s6HKFwX`: Getting Started's
recommended first-run sequence must persist the generated `AWF_API_TOKEN` and
`AWF_POSTGRES_PASSWORD` before setup/start. Package-manager and virtualenv
installs should write `.env`; source-checkout snippets should write
`docker/compose/.env`, matching the later upgrade guide's fresh-shell restore
requirements.

Post-review adjustment for PR thread `PRRT_kwDOSJAM6s6HKQiy`: Quickstart
first-run snippets must persist an `AWF_DATABASE_URL` derived from the same
`AWF_POSTGRES_PASSWORD` that Compose uses to initialize local Postgres. Persist
the matching `AWF_POSTGRES_HOST_PORT` too so custom host ports do not leave the
host-side database URL stale.

Post-review adjustment for PR thread `PRRT_kwDOSJAM6s6HKZLt`: the Quickstart
package-lane first-run block must preserve existing `.env` entries when
persisting AWF-managed local service values. Replace only
`AWF_API_TOKEN`, `AWF_POSTGRES_PASSWORD`, `AWF_POSTGRES_HOST_PORT`, and
`AWF_DATABASE_URL`; do not truncate provider tokens, non-default AWF settings,
or application config before `awf setup` / `awf start`.

1. Update focused docs tests first so current stale docs fail the new lane and
   grammar requirements.
2. Rewrite `docs/QUICKSTART.md` as the canonical lane selector.
3. Update `README.md` to summarize the four lanes and link Quickstart, Upgrade,
   and Uninstall docs.
4. Add `docs/UNINSTALL.md` and expand `docs/UPGRADE.md` with lane-specific
   paths.
5. Align `docs/GETTING_STARTED.md`, `docs/MCP_SETUP.md`, and any narrow public
   index or smoke wording required by focused tests.
6. Update `RELEASING.md` narrowly with release-doc advertising requirements for
   the curl lane.
7. Run the targeted docs tests and focused ruff command from the saved plan.
8. Create `plans/T15_FIRST_RUN_DOCS_VALIDATION.md` with requirement-by-
   requirement status and focused evidence.

## Verification Commands And Pass Criteria

Focused test command:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/docs/test_public_docs_status.py tests/unit/docs/test_troubleshooting_guide.py tests/unit/cli/test_init_parts/test_init_part_004.py -q
```

Focused repair command for PR thread `PRRT_kwDOSJAM6s6HIPd1`:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/docs/test_public_docs_status.py::test_source_checkout_upgrade_docs_refresh_persisted_metadata tests/unit/docs/test_public_docs_status.py::test_upgrade_no_global_source_checkout_rollback_uses_uv_run tests/unit/docs/test_public_docs_status.py::test_upgrade_global_source_checkout_rollback_refreshes_metadata -q
```

Focused repair command for review-level comment `issue:4620140358`:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/docs/test_public_docs_status.py::test_package_upgrade_env_restore_matches_restart_command_line tests/unit/docs/test_public_docs_status.py::test_package_upgrade_docs_restore_service_env_before_start tests/unit/docs/test_public_docs_status.py::test_upgrade_global_source_checkout_rollback_refreshes_metadata -q
```

Focused repair command for review-level comment `issue:4620140358` mixed-doc and
anchor follow-up:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/docs/test_public_docs_status.py::test_quickstart_first_run_urls_match_smoke_defaults tests/unit/docs/test_public_docs_status.py::test_getting_started_first_run_urls_match_smoke_defaults tests/unit/docs/test_public_docs_status.py::test_package_upgrade_env_restore_rejects_prefixed_api_export_line tests/unit/docs/test_public_docs_status.py::test_package_upgrade_docs_restore_service_env_before_start -q
```

Focused repair command for PR thread `PRRT_kwDOSJAM6s6HJDHL`:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/docs/test_public_docs_status.py::test_readme_first_run_grammar_reuses_initialized_project_path -q
```

Focused repair command for PR thread `PRRT_kwDOSJAM6s6HJDHP`:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/docs/test_public_docs_status.py::test_upgrade_release_installed_rollback_restores_service_env_before_start -q
```

Focused repair command for PR thread `PRRT_kwDOSJAM6s6HJ8Ps`:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/docs/test_public_docs_status.py::test_quickstart_source_checkout_first_run_persists_compose_env_for_upgrade -q
```

Post-review adjustment for PR thread `PRRT_kwDOSJAM6s6HKFwb`: source-checkout
uninstall snippets in `docs/UNINSTALL.md` must anchor relative
`docker/compose/.env` and `docker/compose/local-service.yml` paths in the source
checkout before restoring env or stopping Core.

Focused repair command for PR thread `PRRT_kwDOSJAM6s6HKFwb`:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/docs/test_public_docs_status.py::test_uninstall_source_checkout_refresh_requires_core_stop_guidance -q
```

Focused repair command for PR thread `PRRT_kwDOSJAM6s6HKQiy`:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/docs/test_public_docs_status.py::test_quickstart_package_first_run_persists_service_env_for_upgrade tests/unit/docs/test_public_docs_status.py::test_quickstart_source_checkout_first_run_persists_compose_env_for_upgrade tests/unit/docs/test_public_docs_status.py::test_copy_paste_marked_snippets_are_syntactically_valid -q
```

Post-review adjustment for PR thread `PRRT_kwDOSJAM6s6HKggm`: Quickstart
package-lane `.env` preservation must use POSIX/BSD-compatible `sed -e`
delete expressions instead of GNU-only basic `sed` `\|` alternation.

Focused repair command for PR thread `PRRT_kwDOSJAM6s6HKggm`:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/docs/test_public_docs_status.py::test_quickstart_package_first_run_persists_service_env_for_upgrade tests/unit/docs/test_public_docs_status.py::test_copy_paste_marked_snippets_are_syntactically_valid -q
```

Post-review adjustment for PR thread `PRRT_kwDOSJAM6s6HKiIB`: Getting Started
first-run snippets that persist `AWF_POSTGRES_PASSWORD` must also persist the
matching `AWF_POSTGRES_HOST_PORT` and derived `AWF_DATABASE_URL`, so host-side
database checks authenticate with the same password Compose uses for local
Postgres.

Focused repair command for PR thread `PRRT_kwDOSJAM6s6HKiIB`:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/docs/test_public_docs_status.py::test_getting_started_first_run_persists_service_env_for_upgrade tests/unit/docs/test_public_docs_status.py::test_copy_paste_marked_snippets_are_syntactically_valid -q
```

Post-review adjustment for PR thread `PRRT_kwDOSJAM6s6HKuFA`: Getting Started's
package-manager and virtualenv first-run block must preserve existing `.env`
entries when persisting AWF-managed local service values. Replace only
`AWF_API_TOKEN`, `AWF_POSTGRES_PASSWORD`, `AWF_POSTGRES_HOST_PORT`, and
`AWF_DATABASE_URL`; do not truncate provider tokens, non-default AWF settings,
or application config before `awf setup` / `awf start`.

Focused repair command for PR thread `PRRT_kwDOSJAM6s6HKuFA`:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/docs/test_public_docs_status.py::test_getting_started_first_run_persists_service_env_for_upgrade tests/unit/docs/test_public_docs_status.py::test_copy_paste_marked_snippets_are_syntactically_valid -q
```

Post-review adjustment for PR thread `PRRT_kwDOSJAM6s6HKuFE`: README first-run
commands must not let the source checkout with global tool install lane inherit
the release-installed bare `awf setup` / `awf start` startup block. Split that
lane out and pass `--source-checkout "$PWD"` to setup/start so fresh global
source installs use the current checkout's Compose assets even when no
`source_checkout` metadata has been persisted yet.

Focused repair command for PR thread `PRRT_kwDOSJAM6s6HKuFE`:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/docs/test_public_docs_status.py::test_readme_first_run_grammar_reuses_initialized_project_path -q
```

Focused lint command:

```bash
uv run --python 3.12 --extra dev ruff check tests/unit/docs/test_public_docs_status.py tests/unit/docs/test_troubleshooting_guide.py tests/unit/cli/test_init_parts/test_init_part_004.py
```

Post-review adjustment for PR thread `PRRT_kwDOSJAM6s6HK9Wu`: Quickstart
package-lane `.env` preservation must remove old AWF-managed values written as
`export KEY=...` or with leading/key-adjacent whitespace before appending the
remaining `.env`, so the newly printed `AWF_API_TOKEN`,
`AWF_POSTGRES_PASSWORD`, `AWF_POSTGRES_HOST_PORT`, and `AWF_DATABASE_URL`
remain authoritative for later fresh-shell upgrades.

Focused repair command for PR thread `PRRT_kwDOSJAM6s6HK9Wu`:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/docs/test_public_docs_status.py::test_quickstart_package_first_run_strips_exported_awf_env_entries tests/unit/docs/test_public_docs_status.py::test_quickstart_package_first_run_persists_service_env_for_upgrade tests/unit/docs/test_public_docs_status.py::test_copy_paste_marked_snippets_are_syntactically_valid -q
```

Post-review adjustment for PR thread `PRRT_kwDOSJAM6s6HLHOk`: Getting Started
package/virtualenv first-run `.env` preservation must also remove old
AWF-managed values written as `export KEY=...` or with leading/key-adjacent
whitespace before appending the remaining `.env`, matching the Quickstart
package-lane behavior.

Focused repair command for PR thread `PRRT_kwDOSJAM6s6HLHOk`:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/docs/test_public_docs_status.py::test_getting_started_package_first_run_strips_exported_awf_env_entries tests/unit/docs/test_public_docs_status.py::test_getting_started_first_run_persists_service_env_for_upgrade tests/unit/docs/test_public_docs_status.py::test_copy_paste_marked_snippets_are_syntactically_valid -q
```

Post-review adjustment for PR thread `PRRT_kwDOSJAM6s6HLWjk`: source-checkout
upgrade snippets must stop the currently running Core Compose stack before
pulling source changes or reinstalling/syncing the checkout, so `docker compose
... stop` parses the Compose file from the running checkout rather than a newly
pulled file that may require additional environment variables.

Focused repair command for PR thread `PRRT_kwDOSJAM6s6HLWjk`:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/docs/test_public_docs_status.py::test_source_checkout_upgrade_docs_refresh_persisted_metadata -q
```

Pass criteria: the focused commands pass. Full repository tests, full coverage,
OpenAPI drift checks, console builds, push, and PR lifecycle are intentionally
left to AWF/GitHub after agent completion.
