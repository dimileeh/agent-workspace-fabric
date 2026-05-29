# Review PRRT Symlink Config Permissions Plan

## Problem Statement and Scope

Inline review thread `PRRT_kwDOSJAM6s6Fi0EX` reports that
`write_host_setup_config()` treats an explicit symlink path as the standard
`~/.awf/config.yml` path because the identity check resolves symlinks. The
write then tightens permissions on the explicit path's parent even though the
atomic replace overwrites the symlink entry rather than writing through to the
default config target.

Scope is limited to host setup config path identity behavior and focused unit
coverage for explicit symlink paths.

## Requirements Checklist

- Add a regression test proving an explicit symlink to the default config path
  does not chmod the caller-owned parent directory.
- Preserve the existing behavior that writing the actual default config path
  secures the default parent directory.
- Avoid following symlinks when deciding whether an explicit path is the
  standard AWF config path.
- Run only focused validation for the changed host setup config behavior; full
  AWF/GitHub validation remains managed after agent completion.

## Implementation Steps

1. Add a failing unit test in `tests/unit/service/test_host_setup_config.py`
   that creates a shared directory containing a symlink to the default config
   path, writes through that explicit symlink path, and asserts the shared
   directory permissions remain unchanged.
2. Update the standard config identity helper in
   `src/awf/host_setup/config.py` so it compares path names without following
   symlinks.
3. Run the targeted host setup config permission tests.
4. Record validation evidence in the matching validation document.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_host_setup_config.py -k "host_setup_config_write" -q`
  - Passes after the implementation.
  - Before implementation, the new symlink regression should fail on POSIX
    hosts by showing the shared parent permissions were tightened.
