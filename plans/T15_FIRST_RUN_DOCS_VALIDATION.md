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
- Complete: Use `127.0.0.1` for first-run local API and console URLs.
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

Full AWF/GitHub validation, full coverage, OpenAPI drift checks, and frontend
validation were intentionally not run in the agent phase; AWF owns those broad
gates after agent completion.

## Gaps

None.
