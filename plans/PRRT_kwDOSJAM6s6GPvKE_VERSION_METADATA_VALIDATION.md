# PRRT_kwDOSJAM6s6GPvKE Version Metadata Validation

Plan reference:
`plans/PRRT_kwDOSJAM6s6GPvKE_VERSION_METADATA_PLAN.md`

## Requirement Status

- Complete: `awf --version` prefers the installed
  `agent-workspace-fabric` distribution metadata version.
  - Evidence: `src/awf/cli/main.py` now resolves the CLI version with
    `importlib.metadata.version("agent-workspace-fabric")`.
  - Evidence: `tests/unit/cli/test_cli_parts/test_cli_part_001.py` patches the
    distribution metadata version to `9.8.7` and asserts `awf --version` prints
    it instead of the in-source constant.
- Complete: Source checkout or unusual execution layouts without resolvable
  distribution metadata keep a local fallback instead of crashing.
  - Evidence: `src/awf/cli/main.py` catches `PackageNotFoundError` and falls
    back to `awf.__version__`.
  - Evidence: `tests/unit/cli/test_cli_parts/test_cli_part_001.py` covers the
    fallback behavior.
- Complete: Existing fast packaging drift checks remain intact.
  - Evidence: no packaging drift tests were weakened or removed.
- Complete: Validation used targeted checks only.
  - Evidence: broad AWF/GitHub validation was not run; AWF owns that after
    agent completion.

## Commands Run

```bash
uv run --python 3.12 --extra dev pytest \
  tests/unit/cli/test_cli_parts/test_cli_part_001.py \
  -q -k 'root_version_option'
```

Result: passed, `2 passed, 72 deselected`.

```bash
uv run --python 3.12 --extra dev pytest \
  tests/unit/cli/test_cli_parts/test_cli_part_001.py \
  tests/unit/cli/test_packaging.py -q
```

Result: passed, `81 passed`.

```bash
uv run --python 3.12 --extra dev ruff check \
  src/awf/cli/main.py \
  tests/unit/cli/test_cli_parts/test_cli_part_001.py
```

Result: passed.

## Remaining Gaps

None. Full AWF/GitHub validation is managed by AWF after this agent phase.
