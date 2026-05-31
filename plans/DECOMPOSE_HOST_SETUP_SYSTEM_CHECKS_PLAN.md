# Plan — Decompose oversized host_setup system_checks (CI line-limit fix)

## Problem
CI job `python-full-coverage` fails on
`tests/unit/test_core_decomposition_maintainability.py::test_first_party_code_files_stay_under_line_limit`.
Two first-party files exceed the 1,500-line maintainability limit:

- `src/awf/host_setup/system_checks.py` — 2,764 lines
- `tests/unit/service/test_host_setup_system_checks.py` — 3,862 lines

This is a real maintainability-gate bug. Fix = decompose both files; do **not** weaken the check.

## Constraints (from the maintainability suite, applied to ALL first-party files)
- No `_hydrate`, `globals()`, `__dict__.update(`, file-level `# mypy: ignore-errors`,
  broad `# ruff: noqa: E402,F401,F821`, or `_namespace_from_modules`.
- Use plain explicit imports + `__all__` re-export (ruff treats `__all__` names as used).

## Verified facts
- Source consumers of `awf.host_setup.system_checks` import **public** names only:
  `host_setup/__init__.py`, `cli/setup_commands.py`. No private-helper imports in src/scripts.
- Repo decomposition convention: package with submodules + facade; **absolute** intra-package imports.
- Inter-checks references between functional groups are docstring `:func:` mentions only —
  each checks group depends solely on `primitives` (no cycles).
- Tests rely on monkeypatching `system_checks.check_*` (resolved by `run_system_checks`),
  `system_checks.subprocess`/`shutil` (shared stdlib modules), and private helpers via
  `system_checks._foo`.

## Design — convert `system_checks.py` → package `system_checks/`
Keep the import path `awf.host_setup.system_checks` stable. Layered DAG (deps point down):

- `primitives.py` (~340 ln): constants, `SetupCheckLevel`, `PortProbeResult`, `SetupCheckResult`,
  `CommandResult`, `SetupCheckError`, type aliases, probe primitives (`_default_command_runner`,
  `_docker_probe_*`, port/disk/memory probes, `_safe_expanduser`). Deps: stdlib + `service.environment`.
- `checks_core.py` (~550 ln): `check_docker/compose/git/gh/python_runtime/ports/postgres_port/disk/
  shell_path/local_capacity` + `_resolve_path/_resolve_awf_script_dir/_shell_path_fix`. Deps: `primitives`.
- `checks_ports.py` (~745 ln): API/Postgres host-port overrides + conflict + all ollama-bridge checks.
  Deps: `primitives`, `config.DEFAULT_API_HOST_PORT`, `service.environment.env_lookup`.
- `checks_host.py` (~680 ln): work-dir / host-home / auth-mount / required-service-env checks.
  Deps: `primitives`.
- `__init__.py` (~450 ln): **facade + orchestration**. Holds `run_system_checks`, provider
  normalization, `require_interactive`, readiness-payload builder, `KNOWN_SETUP_PROVIDERS`, and the
  public `__all__`. **Rationale:** `run_system_checks`/readiness resolve the `check_*` symbols in
  this namespace, so keeping them here preserves the existing `monkeypatch.setattr(system_checks,
  "check_*", …)` surface and avoids weakening any test. Deps: every submodule + `rendering` +
  `source_assets`.

`__all__` content is preserved byte-for-byte from the original.

## Design — split `test_host_setup_system_checks.py`
Three focused `test_*.py` files (<1,500 ln each) + one shared helper module
`tests/unit/service/_host_setup_system_checks_support.py` (`_command_runner`, `_ok`,
`_stub_non_docker_checks_ok`, `_patch_probes_capture_*`, `_FakeCompleted`, shared imports).
Update only the references that move:
- `system_checks.subprocess`/`shutil` → `primitives.subprocess`/`primitives.shutil`.
- Private helpers via submodule: `primitives._docker_probe_environ/_default_command_runner/
  _default_*_probe/_safe_expanduser/_loopback_port_probe`; `checks_core._resolve_path/_shell_path_fix`;
  `checks_ports._ollama_bridge_profile_enabled`; `checks_host._resolve_work_dir`.
- `system_checks.check_*` monkeypatches stay unchanged (resolved in package `__init__`).
All 161 tests preserved; no assertions altered.

## Execution
1. Source package via byte-exact `sed` body extraction + tailored import headers. (Commit 1)
2. Verify: import smoke (all `__all__` + moved privates), focused ruff + mypy, maintainability test.
3. Test split via byte-exact extraction + shared helper module + ref updates. (Commit 2)
4. Verify: full focused test file suite (161 tests), maintainability test, ruff, mypy.
5. Workflow: adversarial review + multi-dimensional verification of the final diff
   (nothing dropped, no weakening, public API + `__all__` unchanged).

Broad/CI-equivalent validation is owned by AWF/GitHub after the agent phase.
