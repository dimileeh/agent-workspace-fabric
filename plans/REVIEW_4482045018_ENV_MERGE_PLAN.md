# Review 4482045018 Env Merge Plan

## Problem Statement And Scope

Address the review-level comment on PR #264 about `awf init` env seeding. The review identified two risks in `src/awf/cli/main.py`:

- Root `.env` keys that exist only in the overlay are appended to `docker/compose/.env` without an operator-visible audit trail.
- A single leading comment before any seed-shared overlay key is treated as a file header, which can move key-specific comments away from the key they document.

Scope is limited to the env seeding merge/audit path and focused unit coverage.

## Requirements Checklist

- Add a regression test proving overlay-only key names are reported without leaking values.
- Add a regression test proving a single leading comment before a seed-shared key remains attached to that key instead of becoming a file header.
- Preserve existing env merge behavior for comments, ordering, duplicate root-only keys, and no value leakage.
- Keep JSON/pretty output machine/operator-auditable where env seeding succeeds.
- Validate with the narrowest relevant unit tests and lint/type checks when practical.

## Implementation Steps

1. Add failing unit tests in `tests/unit/cli/test_init.py` for overlay-only key audit output and single-comment header detection.
2. Extend the merge implementation to collect overlay-only key names while preserving the public byte-returning helper for existing tests.
3. Thread the audit metadata through `_seed_env_file` and `awf init` output without printing secret values.
4. Remove the `key in seed_keys` header shortcut so `_env_context_looks_like_file_header` is the single source of header classification.
5. Run targeted tests, then broader validation if the touched surface warrants it.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/cli/test_init.py -q`
- `uv run --python 3.12 --extra dev ruff check src/awf tests/unit/cli/test_init.py`
- `uv run --python 3.12 --extra dev mypy src/awf`

Pass criteria: targeted tests pass, lint/type checks pass, and no secret values are emitted in the new audit output.
