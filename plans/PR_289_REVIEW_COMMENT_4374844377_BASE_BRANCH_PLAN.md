# PR 289 Review Comment 4374844377 Base Branch Plan

## Problem Statement and Scope

CodeRabbit reported that companion task-policy loading treats optional
`base_branch` incorrectly: a missing key raises `KeyError`, and a JSON `null`
value becomes the literal string `"None"`. The public companion request contract
allows omission and documents that companion `base_branch` defaults to the
primary workspace base branch.

Scope is limited to companion task-policy loading and the provisioning checkout
fallback needed for an omitted companion base branch.

## Requirements Checklist

- Preserve omitted companion `base_branch` as `None` in the normalized runtime
  spec.
- Preserve explicit JSON `null` companion `base_branch` as `None`.
- Keep explicit non-null `base_branch` values as strings.
- Use the parent workspace `branch_base` when provisioning a companion whose
  loaded spec has `base_branch=None`.
- Add focused regression coverage for the omitted/null loader path and
  provisioning fallback.
- Run only focused local checks; AWF/GitHub own broad validation after agent
  completion.

## Implementation Steps

1. Add focused unit tests that describe omitted/null companion `base_branch`
   loader behavior.
2. Add a focused provisioner unit test for the fallback from missing companion
   `base_branch` to the parent workspace base branch.
3. Confirm the new tests fail against current code when practical.
4. Update the companion runtime spec and loader to use safe access and preserve
   `None`.
5. Update provisioner companion materialization to checkout from the parent
   workspace base branch when a companion spec omits `base_branch`.
6. Run targeted tests for the changed companion/provisioner behavior.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/node/test_companion_services.py -q`
  passes.
- `uv run --python 3.12 --extra dev pytest tests/unit/node/test_provisioner_parts/test_provisioner_part_001.py -q -k companion`
  passes.

Full AWF/GitHub validation is managed by AWF after this agent phase.
