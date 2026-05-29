# Comment 4571563982 First-Run Rendering Plan

## Problem Statement and Scope

Address PR review comment `issue:4571563982` for first-run rendering. The review identifies two style-level correctness risks in `src/awf/host_setup/rendering.py`:

- Provider reference redaction currently matches known schemes inside hyphenated URI-like prefixes such as `x-plain-file://...`, leaving a partial `x-[redacted]` token.
- `render_first_run_json()` iterates `raw_payload.get("issues", [])` even though `model_dump(mode="python")` emits the `issues` field as a tuple.

Scope is limited to first-run rendering code, focused regression coverage, this plan, and the matching validation document.

## Requirements Checklist

- Add or update focused tests for provider reference boundary behavior before changing the implementation.
- Redact hyphen-prefixed provider-reference-like tokens as one complete token, while preserving existing assignment redaction such as `TOKEN=env://...`.
- Keep non-provider concatenations such as `safeplain-file://...` from being treated as provider references.
- Make the `issues` iteration contract explicit by using a tuple default/contract rather than a list default.
- Run only targeted tests for the changed rendering behavior; broad AWF/GitHub validation remains managed after agent completion.
- Commit the local fix on the current AWF-managed branch without pushing or switching branches.

## Implementation Steps

1. Add a unit test in `tests/unit/service/test_host_setup_rendering.py` for hyphen-prefixed provider ref redaction and assignment preservation.
2. Run that targeted test to confirm it fails against the current regex.
3. Update `_PROVIDER_REF_RE` and any nearby comment to describe explicit scheme-token boundary semantics.
4. Update `render_first_run_json()` to iterate `raw_payload.get("issues") or ()` and assert the tuple contract from `model_dump(mode="python")`.
5. Run the focused host setup rendering tests that cover the changed paths.
6. Record the result in `plans/COMMENT_4571563982_FIRST_RUN_RENDERING_VALIDATION.md`.
7. Stage only changed files and commit with a conventional commit message for comment `4571563982`.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_host_setup_rendering.py -q`

Pass criteria: targeted rendering tests pass locally. Full AWF/GitHub validation, broad lint/type checks, and coverage gates are intentionally left to AWF after agent completion per workspace contract.
