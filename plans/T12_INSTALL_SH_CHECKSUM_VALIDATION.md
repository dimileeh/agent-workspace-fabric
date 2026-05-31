# T12 — Checked-in `install.sh` With Checksum Verification (Validation)

Plan reference:
`plans/T12_INSTALL_SH_CHECKSUM_PLAN.md`

Source implementation contract:

- `docs/awf-plans/ws_e47a804a47d6483d8fef6f44.md`
- `plans/AWF_FULL_INSTALLER_FIRST_RUN_SETUP_PLAN.md`
- `TODO/awf-full-installer-first-run-setup-backlog.md`

Implementation under validation: `packaging/install.sh`, exercised by the
black-box subprocess suite under `tests/unit/installer/`.

## Requirement Status (Behavior)

| Req | Status | Evidence |
| --- | --- | --- |
| R1 CLI flags (`--version`, `--channel`, `--method`, `--install-dir`, `--dry-run`, `--uninstall`, `--help`, plus `--shell` seam). | Complete | `parse_args` (install.sh:119-194) handles each flag in both space and `=` forms; `usage` (install.sh:80-106) documents them. `test_install_sh_syntax.py::test_help_lists_every_documented_flag` asserts `--help` lists every documented flag; `test_install_sh_path_advice.py` drives `--shell`. |
| R2 Platform guard before any download/install/uninstall; otherwise `UNSUPPORTED_PLATFORM`. | Complete | `detect_platform` (install.sh:200-217) is called from `main` (install.sh:660) before both `run_install` and `uninstall_awf`. `test_install_sh_platform.py::test_unsupported_platform_fails_before_mutation`, `::test_supported_platforms_pass_the_guard`, and `test_install_sh_uninstall.py::test_uninstall_aborts_on_unsupported_platform_before_mutation` cover OS/arch rejection and the pre-uninstall guard. |
| R3 Manifest resolution honoring `AWF_INSTALL_MANIFEST` / `AWF_INSTALL_BASE_URL`, with `MANIFEST_UNAVAILABLE` / `MANIFEST_INVALID`. | Complete | `resolve_manifest` (install.sh:275-293) prefers `AWF_INSTALL_MANIFEST`, falls back to `AWF_INSTALL_BASE_URL`/default repo, and emits `MANIFEST_UNAVAILABLE` on missing/empty manifest; `parse_manifest` (install.sh:299-335) emits `MANIFEST_INVALID`. `test_install_sh_install.py::test_missing_manifest_source_is_unavailable` and `::test_manifest_without_wheel_artifact_is_invalid` cover both tokens. |
| R4 jq-free portable manifest parsing for the wheel `name`/`url`/`sha256`. | Complete | `parse_manifest` uses an `awk`/`sed` parser keyed on the manifest's stable sorted-key shape (install.sh:299-335); no `jq` dependency. Exercised indirectly by every install/dry-run test that resolves a fixture manifest. |
| R5 Fetch helper for `https://`, `http://` (curl/wget) and `file://`/local path (`cp`); `DOWNLOAD_FAILED` on failure. | Complete | `fetch` (install.sh:246-269) dispatches on scheme; `download_artifact` (install.sh:359-363) maps failure/empty to `DOWNLOAD_FAILED`. The hermetic harness serves artifacts over `file://`/local paths; install and dry-run tests rely on this path. |
| R6 sha256 verification; mismatch prints `CHECKSUM_MISMATCH` + URL and aborts before any install mutation. | Complete | `verify_checksum` (install.sh:380-391) compares case-folded digests and fails with `CHECKSUM_MISMATCH`; `run_install` (install.sh:635-637) verifies before `install_artifact`. `test_install_sh_checksum.py::test_checksum_mismatch_aborts_before_install` and `::test_checksum_mismatch_aborts_even_in_dry_run` assert no install command runs on mismatch. |
| R7 Install via locally verified wheel: `uv tool install` default, `pipx install` on `--method pipx`; `INSTALL_METHOD_FAILED` preserves tool stderr. | Complete | `install_uv` / `install_pipx` (install.sh:404-424) install from the verified `ARTIFACT_FILE` and map tool failure to `INSTALL_METHOD_FAILED`. `test_install_sh_install.py::test_install_method_failure_preserves_reason_and_tool_stderr` and `::test_pipx_method_uses_pipx_not_uv` cover both. |
| R8 Post-install reachability check: `AWF_NOT_REACHABLE` + exact shell fix; never claim success on an unreachable binary. | Complete | `verify_awf` (install.sh:516-555) resolves the installed binary (preferring the install dir over a PATH shadow), runs `--help`, and fails `AWF_NOT_REACHABLE` otherwise. Covered by `test_install_sh_install.py::test_reachability_failure_does_not_claim_success`, `::test_default_install_verifies_installed_binary_not_path_shadow`, `::test_default_install_binary_verified_even_when_path_awf_is_broken`, `::test_install_dir_*`, and `::test_uv_install_uses_uv_tool_bin_dir_for_reachability` / `::test_pipx_install_uses_pipx_bin_dir_for_reachability`. |
| R9 Per-shell PATH advice (zsh/bash/fish) when the install dir is not on `PATH`. | Complete | `print_path_advice` (install.sh:475-514) renders zsh/bash/fish rc lines (and surfaces the bash login profile); `default_bin_dir` (install.sh:430-462) resolves the real uv/pipx bin dir. `test_install_sh_path_advice.py::test_path_advice_matches_shell`, `::test_bash_path_advice_includes_login_profile_when_present`, and `::test_no_path_advice_when_awf_already_on_path` cover it. |
| R10 `--dry-run` resolves + downloads + verifies, prints ordered planned actions, never installs or writes rc files. | Complete | `run_install` (install.sh:639-644) short-circuits after checksum verification, emitting ordered `[plan]` lines and no mutation. `test_install_sh_dry_run.py::test_dry_run_explains_ordered_plan_without_mutation` and `::test_dry_run_reports_the_resolved_wheel_name` assert ordering and no install command. |
| R11 `--uninstall` removes only AWF-managed (uv/pipx-reported) installs; refuses unknown executables with `UNINSTALL_REFUSED_UNMANAGED` and deletes nothing. | Complete | `uninstall_awf` (install.sh:596-617) uses anchored `uv_lists_package` / `pipx_lists_package` token matches (install.sh:561-582) and refuses unmanaged binaries. `test_install_sh_uninstall.py` covers managed uv/pipx uninstall, unmanaged refusal, the no-op case, and substring-fork rejection. |
| R12 `MISSING_DEPENDENCY` for absent download/sha256/install tools; `BAD_USAGE` for unknown flags / invalid `--channel` / `--method`. | Complete | `need_download_tool` (install.sh:234-242), `compute_sha256` (install.sh:369-378), and the `command -v` guards in `install_uv`/`install_pipx` emit `MISSING_DEPENDENCY`; `bad_usage` (install.sh:75-78) plus the `--channel`/`--method` validation (install.sh:185-193) emit `BAD_USAGE`. `test_install_sh_syntax.py::test_unknown_flag_is_a_bad_usage_error`, `test_install_sh_install.py::test_invalid_method_is_a_bad_usage_error`, and `::test_invalid_channel_is_a_bad_usage_error` cover the usage path. |
| R13 bash 3.2 compatible, `set -euo pipefail`, no bash-4 features, jq-free. | Complete | `set -euo pipefail` at install.sh:24; no associative arrays / `${var^^}` / `mapfile`; case-folding uses `tr`; parsing uses `awk`/`sed`. `test_install_sh_syntax.py::test_install_sh_passes_bash_syntax_check` runs `bash -n`, and `::test_install_sh_is_a_checked_in_executable` asserts the script is checked in and executable. |

## Requirement Status (Tests — strict TDD)

| Planned artifact | Status | Evidence |
| --- | --- | --- |
| `tests/unit/installer/conftest.py` (fixtures, fake wheel, manifest factory, stub bin builder, hermetic runner). | Complete | `tests/unit/installer/conftest.py` provides the installer path, fake-wheel/manifest factories, stub-bin builder, and `InstallerHarness`. |
| `test_install_sh_syntax.py` (`bash -n` + `--help` lists every flag). | Complete | Present; 4 tests including `bash -n`, checked-in/executable, help-flag coverage, and unknown-flag usage. |
| `test_install_sh_dry_run.py` (ordered actions, no mutation). | Complete | Present; ordered-plan and resolved-wheel-name tests. |
| `test_install_sh_checksum.py` (mismatch aborts before install). | Complete | Present; mismatch aborts in both normal and dry-run paths. |
| `test_install_sh_platform.py` (unsupported OS + arch before mutation). | Complete | Present; unsupported-platform and supported-platform-guard tests. |
| `test_install_sh_install.py` (install-method failure, pipx fallback, reachability, missing-dependency / manifest-invalid edges). | Complete | Present; covers all listed edges plus install-dir and bin-dir reachability hardening. |
| `test_install_sh_path_advice.py` (zsh/bash/fish advice). | Complete | Present; per-shell advice, bash login profile, and no-advice-when-on-PATH. |
| `test_install_sh_uninstall.py` (managed uninstall + unmanaged refusal). | Complete | Present; managed uv/pipx, unmanaged refusal, no-op, substring-fork, and pre-uninstall platform guard. |

## Assumptions / Changes Vs Plan

- **Added `--channel` enforcement and a `CHANNEL_MISMATCH` reason token** beyond
  the plan's original token list. The plan listed `--channel` as a flag (R1) and
  `MANIFEST_INVALID`-class guards, but review feedback on this PR hardened
  channel handling: when no `--version` pins the release, `verify_channel`
  (install.sh:350-357) fails `CHANNEL_MISMATCH` if the resolved manifest's
  channel differs from the requested one; a pinned `--version` makes the channel
  field informational (the tag is the trust boundary). This is covered by the
  added `tests/unit/installer/test_install_sh_channel.py` (4 tests). The token is
  documented in the script header (install.sh:16).
- All other requirements were implemented as planned; no requirement was dropped
  or deferred.

## Changed Files (T12 slice)

- `packaging/install.sh`
- `tests/unit/installer/conftest.py`
- `tests/unit/installer/test_install_sh_syntax.py`
- `tests/unit/installer/test_install_sh_dry_run.py`
- `tests/unit/installer/test_install_sh_checksum.py`
- `tests/unit/installer/test_install_sh_platform.py`
- `tests/unit/installer/test_install_sh_install.py`
- `tests/unit/installer/test_install_sh_path_advice.py`
- `tests/unit/installer/test_install_sh_uninstall.py`
- `tests/unit/installer/test_install_sh_channel.py`
- `plans/T12_INSTALL_SH_CHECKSUM_PLAN.md`
- `plans/T12_INSTALL_SH_CHECKSUM_VALIDATION.md`

## Verification Evidence (focused)

Syntax check:

```bash
bash -n packaging/install.sh
```

Result: exit 0 (clean).

Focused installer suite:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/installer -q
```

Result: `49 passed in 1.44s`.

Broad AWF/GitHub validation — full repository test suite, the 99% coverage gate,
ruff/mypy across the tree, OpenAPI drift, frontend builds, push, PR creation, and
PR monitoring — was **not** run in the agent phase. Those are owned by AWF and
GitHub CI after agent completion per the workspace contract. The shell installer
is outside `source = ["src/awf"]`, so it does not affect Python coverage.

## Remaining Gaps

None for the T12 slice. Out-of-scope items remain owned elsewhere per the plan's
Non-Goals: T13 package-data inclusion, T15 docs, T16 release-workflow installer
smoke, T03 Python first-run catalog, and release signing.
