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
- Use `127.0.0.1` for local API and console URLs in first-run guidance.
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

Focused lint command:

```bash
uv run --python 3.12 --extra dev ruff check tests/unit/docs/test_public_docs_status.py tests/unit/docs/test_troubleshooting_guide.py tests/unit/cli/test_init_parts/test_init_part_004.py
```

Pass criteria: both focused commands pass. Full repository tests, full coverage,
OpenAPI drift checks, console builds, push, and PR lifecycle are intentionally
left to AWF/GitHub after agent completion.
