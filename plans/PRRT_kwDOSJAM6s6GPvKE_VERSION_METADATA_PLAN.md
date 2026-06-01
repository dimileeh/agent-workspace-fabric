# PRRT_kwDOSJAM6s6GPvKE Version Metadata Plan

## Problem Statement and Scope

The root `awf --version` option currently prints `awf.__version__`. Review
thread `PRRT_kwDOSJAM6s6GPvKE` reports that release installs can fail the
installer post-check when `pyproject.toml` and `src/awf/__init__.py` drift,
because the wheel and install manifest use distribution metadata while the CLI
uses the stale in-source constant.

Scope is limited to the CLI version callback and its focused unit coverage.

## Requirements Checklist

- `awf --version` must prefer the installed `agent-workspace-fabric`
  distribution metadata version.
- Source checkout or unusual execution layouts without resolvable distribution
  metadata must keep a local fallback instead of crashing.
- Existing fast packaging drift checks must remain intact.
- Validate with targeted CLI tests only; broad AWF/GitHub validation is managed
  after agent completion.

## Implementation Steps

1. Add a failing unit test that patches distribution metadata to a different
   version and expects `awf --version` to print that metadata version.
2. Add a focused fallback unit test for missing distribution metadata.
3. Update `src/awf/cli/main.py` to resolve the CLI version through
   `importlib.metadata.version("agent-workspace-fabric")`, falling back to
   `awf.__version__` only when metadata is unavailable.
4. Run the targeted CLI test module or individual relevant tests.

## Verification Commands and Pass Criteria

```bash
uv run --python 3.12 --extra dev pytest \
  tests/unit/cli/test_cli_parts/test_cli_part_001.py \
  tests/unit/cli/test_packaging.py -q
```

Pass criteria: targeted tests pass. Full AWF/GitHub validation remains owned by
AWF after this agent phase.
