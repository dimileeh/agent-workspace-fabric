# T14 Clean-Install And Source-Lane Smoke Harness Validation

Plan reference: `plans/T14_CLEAN_INSTALL_SOURCE_LANES_PLAN.md`

## Requirement Status

- Complete: Added `scripts/first_run_smoke.py` with lanes for
  `installer-fixture`, `installer-release`, `tool-install`,
  `source-tool-install`, and `source-uv-run`.
- Complete: Default local lanes run without a published release
  (`installer-fixture`, `source-uv-run`).
- Complete: Release lane is disabled unless `--allow-release`,
  `--release-dist-dir`, and `--release-manifest` are all provided.
- Complete: Installer fixture lane writes a local `file://` manifest/artifact
  fixture and runs `packaging/install.sh --dry-run`.
- Complete: `uv` and `pipx` tool environments pin `HOME`, tool home, tool bin,
  cache, XDG directories, and `PATH` under the temp smoke root.
- Complete: Source checkout copy preserves AWF source markers and excludes
  `.git`, virtualenvs, caches, build/dist output, and package metadata.
- Complete: Source no-global lane runs `uv run --project <copied-checkout>`
  from an outside cwd with temp `HOME`, scrubbed `PYTHONPATH`, and explicit
  `--source-checkout`.
- Complete: Source global lane runs `uv tool install . --force` from a temp
  copied checkout into isolated tool dirs, then runs installed `awf` help/setup
  commands from outside the checkout.
- Complete: Focused unit and integration coverage was added for lane wiring,
  release gating, isolated envs, source copy behavior, and source-lane execution.
- Complete: Broad docs were not edited; T15 remains the broader docs owner.

## Files Changed

- `docs/awf-plans/ws_03f9dfb2e6254c05a8cad2a2.md` - AWF-provided saved
  implementation contract used for this workspace.
- `plans/T14_CLEAN_INSTALL_SOURCE_LANES_PLAN.md` - protocol implementation plan.
- `plans/T14_CLEAN_INSTALL_SOURCE_LANES_VALIDATION.md` - this validation record.
- `scripts/first_run_smoke.py` - first-run smoke harness.
- `tests/unit/scripts/test_first_run_smoke.py` - unit coverage for harness
  command/env/gating behavior.
- `tests/integration/test_first_run_smoke.py` - focused source-lane integration
  coverage.

## Focused Verification

Passed:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/scripts/test_first_run_smoke.py -q
uv run --python 3.12 --extra dev pytest tests/unit/scripts/test_release_smoke.py -q
uv run --python 3.12 --extra dev pytest tests/integration/test_first_run_smoke.py -q
uv run --python 3.12 --extra dev ruff check scripts/first_run_smoke.py tests/unit/scripts/test_first_run_smoke.py tests/integration/test_first_run_smoke.py
uv run --python 3.12 --extra dev ruff format --check scripts/first_run_smoke.py tests/unit/scripts/test_first_run_smoke.py tests/integration/test_first_run_smoke.py
uv run --python 3.12 --extra dev python scripts/first_run_smoke.py --lane installer-fixture --lane source-uv-run --checkout-root .
```

The harness CLI reported all requested fixture/source no-global commands as
`PASSED`.

The local `git commit` hook also ran automatically and passed its configured
checks, including ruff, ruff format, and mypy.

## Broad Validation Boundary

Per the AWF workspace contract, I did not run full-repository pytest, full
coverage gates, frontend builds, or the full `.awf/workspace.yml` validation
suite. AWF/GitHub own broad validation, provenance, timeouts, and merge gating
after agent completion.
