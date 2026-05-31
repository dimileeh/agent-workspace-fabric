# T12 — Checked-in `install.sh` With Checksum Verification (Implementation Plan)

Repo protocol artifact for the implementation phase of task **T12**. Translates the
planning artifact `docs/awf-plans/ws_e47a804a47d6483d8fef6f44.md` into an executable
checklist. Validation lives in `plans/T12_INSTALL_SH_CHECKSUM_VALIDATION.md`.

## Problem Statement And Scope

Add an inspected, checked-in shell installer at `packaging/install.sh` that installs the
`agent-workspace-fabric` package from a manifest-pinned, sha256-verified wheel. The
installer consumes the T11 install manifest (`awf-install-manifest.json`), verifies the
artifact checksum **before** any install mutation, installs via a locally verified wheel
(`uv tool install` default, `pipx install` fallback), verifies `awf` reachability, and
prints correct per-shell PATH advice. It also supports `--dry-run` and a conservative
`--uninstall` that refuses unmanaged executables.

The installer is a standalone bash script. It must not depend on the Python first-run error
catalog (T03) and emits its own stable shell-level reason tokens.

## Requirements Checklist

Behavior:

- [ ] R1 CLI flags: `--version`, `--channel`, `--method`, `--install-dir`, `--dry-run`,
  `--uninstall`, `--help` (plus `--shell` testability seam).
- [ ] R2 Platform guard: `uname -s ∈ {Darwin, Linux}` and `uname -m` in an arch allowlist;
  otherwise `UNSUPPORTED_PLATFORM` and exit **before** any download/install.
- [ ] R3 Manifest resolution honoring `AWF_INSTALL_MANIFEST` and `AWF_INSTALL_BASE_URL`,
  with `MANIFEST_UNAVAILABLE` / `MANIFEST_INVALID` reason tokens.
- [ ] R4 jq-free portable manifest parsing for the wheel artifact `name`/`url`/`sha256`.
- [ ] R5 Fetch helper supporting `https://`, `http://` (curl/wget) and `file://`/local path
  (`cp`); `DOWNLOAD_FAILED` on failure.
- [ ] R6 sha256 verification (`sha256sum` or `shasum -a 256`) — checksum mismatch prints
  `CHECKSUM_MISMATCH` + artifact URL and exits **before** any install mutation.
- [ ] R7 Install via locally verified wheel: `uv tool install` default, `pipx install`
  when `--method pipx`; `INSTALL_METHOD_FAILED` preserves the underlying tool stderr.
- [ ] R8 Post-install reachability check: `AWF_NOT_REACHABLE` + exact shell fix on failure;
  never claim success on an unreachable binary.
- [ ] R9 Per-shell PATH advice (zsh/bash/fish) when the install dir is not on `PATH`.
- [ ] R10 `--dry-run` resolves + downloads + verifies, prints the ordered planned actions,
  and never calls the install method or writes shell rc files.
- [ ] R11 `--uninstall` removes only AWF-managed installs (uv/pipx-reported); refuses
  unknown executables with `UNINSTALL_REFUSED_UNMANAGED` and performs no deletion.
- [ ] R12 `MISSING_DEPENDENCY` for absent download/sha256/install tools; `BAD_USAGE` for
  unknown flags / invalid `--channel` / `--method`.
- [ ] R13 bash 3.2 compatible (macOS), `set -euo pipefail`, no bash-4 features, jq-free.

Tests (strict TDD — written/red before the script exists):

- [ ] `tests/unit/installer/conftest.py` — fixtures (installer path, fake wheel, manifest
  factory, stub bin builder, hermetic runner).
- [ ] `test_install_sh_syntax.py` — `bash -n` + `--help` lists every flag.
- [ ] `test_install_sh_dry_run.py` — macOS + Linux dry-run, ordered actions, no mutation.
- [ ] `test_install_sh_checksum.py` — mismatch aborts before install.
- [ ] `test_install_sh_platform.py` — unsupported OS + unsupported arch before mutation.
- [ ] `test_install_sh_install.py` — install-method failure, pipx fallback, reachability
  failure, plus missing-dependency / manifest-invalid edges.
- [ ] `test_install_sh_path_advice.py` — zsh/bash/fish advice.
- [ ] `test_install_sh_uninstall.py` — managed uninstall + unmanaged refusal.

## Implementation Steps (smallest green path)

1. Create this plan (done).
2. Add `tests/unit/installer/conftest.py` and failing black-box subprocess tests.
3. Confirm red (script absent).
4. Implement `packaging/install.sh` in trust order: arg/usage → platform guard →
   manifest resolve/parse → fetch → sha256 verify → install (uv default, pipx fallback) →
   reachability + PATH advice → uninstall guard → dry-run gating.
5. Re-run focused tests + `bash -n` until green.
6. Write `plans/T12_INSTALL_SH_CHECKSUM_VALIDATION.md` mapping every requirement and
   failure mode to evidence.
7. Commit locally with a scoped message. Do not push/rebase; AWF owns push + PR.

## Stable Reason Tokens (shell-owned contract)

`UNSUPPORTED_PLATFORM`, `MISSING_DEPENDENCY`, `MANIFEST_UNAVAILABLE`, `MANIFEST_INVALID`,
`DOWNLOAD_FAILED`, `CHECKSUM_MISMATCH`, `INSTALL_METHOD_FAILED`, `AWF_NOT_REACHABLE`,
`UNINSTALL_REFUSED_UNMANAGED`, `BAD_USAGE`.

## Verification Commands And Pass Criteria

```bash
bash -n packaging/install.sh
uv run --python 3.12 --extra dev pytest tests/unit/installer -q
uv run --python 3.12 --extra dev ruff check src/awf tests
uv run --python 3.12 --extra dev mypy src/awf
```

Pass criteria: `bash -n` exits 0; all installer tests pass; ruff/mypy clean. Broad
repo/coverage/CI validation is owned by AWF and GitHub after the agent phase
(`source = ["src/awf"]` coverage is unaffected by the shell script).

## Non-Goals (owned elsewhere)

- T13 package-data inclusion of the installer/assets.
- T16 release-workflow manifest/checksum/installer-smoke enforcement.
- T15 final README/Quickstart/upgrade/uninstall docs.
- T03 Python first-run error catalog / reason-catalog edits.
- Release signing / signature verification (reserved; v1 trust is sha256 only).
- Windows-native installer, Homebrew advertising, MCP/credential/provider surfaces.
