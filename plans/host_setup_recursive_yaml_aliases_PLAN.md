# Host Setup Recursive YAML Aliases Plan

## Problem Statement and Scope

An unresolved review thread reports that YAML alias cycles such as
`audit: &a [*a]` can make the host setup config secret-payload scan recurse
until Python raises `RecursionError`. That escapes the host setup config
reason-code contract. The fix is limited to host setup config parsing and the
focused unit coverage for that behavior.

## Requirements Checklist

- Add a regression test showing a recursive YAML alias is reported as
  `HOST_SETUP_CONFIG_CORRUPT`.
- Preserve existing secret-key and secret-value rejection behavior and
  sanitized diagnostics.
- Prevent recursive container traversal in `_ensure_no_secret_payload`.
- Keep validation focused; full AWF/GitHub validation is managed after agent
  completion.

## Implementation Steps

1. Add a focused failing unit test in `tests/unit/service/test_host_setup_config.py`.
2. Update `src/awf/host_setup/config.py` so recursive mappings/sequences are
   detected before descent and represented as corrupt config during reads.
3. Run the targeted regression test, then the relevant focused host setup
   config tests.
4. Record validation evidence in a matching validation document.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_host_setup_config.py -q`
  passes.
- A pre-fix targeted run of the new regression is expected to fail with the
  current `RecursionError` behavior.
