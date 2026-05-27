# PR 289 Review Comment 4374844377 Base Branch Validation

Plan reference:
`plans/PR_289_REVIEW_COMMENT_4374844377_BASE_BRANCH_PLAN.md`

## Requirement Status

- Preserve omitted companion `base_branch` as `None` in the normalized runtime
  spec: Complete.
- Preserve explicit JSON `null` companion `base_branch` as `None`: Complete.
- Keep explicit non-null `base_branch` values as strings: Complete.
- Use the parent workspace `branch_base` when provisioning a companion whose
  loaded spec has `base_branch=None`: Complete.
- Add focused regression coverage for the omitted/null loader path and
  provisioning fallback: Complete.
- Run only focused local checks; AWF/GitHub own broad validation after agent
  completion: Complete.

## Evidence

Files changed:

- `src/awf/node/companion_services.py`
- `src/awf/node/provisioner.py`
- `tests/unit/node/test_companion_services.py`
- `tests/unit/node/test_provisioner_parts/test_provisioner_part_001.py`
- `plans/PR_289_REVIEW_COMMENT_4374844377_BASE_BRANCH_PLAN.md`
- `plans/PR_289_REVIEW_COMMENT_4374844377_BASE_BRANCH_VALIDATION.md`

Focused red/green evidence:

- Before implementation,
  `uv run --python 3.12 --extra dev pytest tests/unit/node/test_companion_services.py -q -k base_branch`
  failed with `KeyError: 'base_branch'` for an omitted value and `"None"` for an
  explicit null value.
- Before implementation,
  `uv run --python 3.12 --extra dev pytest tests/unit/node/test_provisioner_parts/test_provisioner_part_001.py -q -k companion`
  failed when a persisted companion omitted `base_branch`.

Focused validation commands:

- `uv run --python 3.12 --extra dev pytest tests/unit/node/test_companion_services.py -q`
  passed: 16 passed.
- `uv run --python 3.12 --extra dev pytest tests/unit/node/test_provisioner_parts/test_provisioner_part_001.py -q -k companion`
  passed: 1 passed, 44 deselected.
- `uv run --python 3.12 --extra dev ruff check src/awf/node/companion_services.py src/awf/node/provisioner.py tests/unit/node/test_companion_services.py tests/unit/node/test_provisioner_parts/test_provisioner_part_001.py`
  passed.
- `uv run --python 3.12 --extra dev mypy src/awf/node/companion_services.py src/awf/node/provisioner.py`
  passed.

Full AWF/GitHub validation is managed by AWF after this agent phase.

## Gaps

None.
