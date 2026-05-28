# Review PRRT_kwDOSJAM6s6FhzOz Default Config Permissions Plan

## Problem Statement And Scope

The host setup config writer only tightens the parent directory to `0700` when
`write_host_setup_config()` is called without an explicit path. Review feedback
reports that explicit paths that still name the standard AWF config location,
for example `default_host_setup_config_path(home=...)` or an expanded
`~/.awf/config.yml`, can leave a newly created `.awf` directory at the process
umask mode such as `0755`.

Scope is limited to host setup config path permission behavior and its focused
unit coverage.

## Requirements Checklist

- Add a regression test showing an explicit default AWF config path gets a
  `0700` parent directory when created under a permissive umask.
- Preserve the existing behavior that arbitrary explicit parent directories are
  not chmodded to `0700`.
- Keep config file writes at `0600`.
- Run focused validation only for the touched host setup config tests; broad AWF
  and GitHub validation remain managed by AWF after agent completion.

## Implementation Steps

1. Add the failing regression in `tests/unit/service/test_host_setup_config.py`.
2. Update `src/awf/host_setup/config.py` to identify standard AWF config paths
   by path shape, not only by path omission.
3. Re-run the focused regression and nearby config permission tests.
4. Record validation evidence in the matching validation document.
