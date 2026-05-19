# Review 4491715538 Validation

Plan reference: `plans/REVIEW_4491715538_PLAN.md`

## Requirement Status

- Complete: Added regression coverage proving informational workflow additions
  using `recovery`, `discover`, and POSIX `test -f` commands are not classified
  as validation commands.
- Complete: Added regression coverage proving pinned action semantic version
  downgrades are blocked.
- Complete: Preserved allowed same-action pinned version upgrades and existing
  SHA transition behavior.
- Complete: Kept conservative behavior for other protected workflow changes by
  limiting implementation to validation command matching and pinned ref bump
  classification.

## Evidence

Files changed:

- `src/awf/control/quality_gates.py`
- `tests/unit/control/test_quality_gates.py`
- `plans/REVIEW_4491715538_PLAN.md`
- `plans/REVIEW_4491715538_VALIDATION.md`

Commands run:

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_quality_gates.py -q -k "non_validation_command_words or version_downgrade"` failed before implementation with the expected four regression failures.
- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_quality_gates.py -q -k "non_validation_command_words or version_downgrade or version_upgrade"` passed after implementation.
- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_quality_gates.py -q` passed.
- `uv run --python 3.12 --extra dev ruff check src/awf tests` passed.
- `uv run --python 3.12 --extra dev mypy src/awf` passed.

Additional note: a full `uv run --python 3.12 --extra dev pytest tests/unit -q`
sweep was started after the targeted checks, but stopped after several minutes
because it was still near the beginning of the suite. The focused quality-gate
module plus lint and mypy are the completed pass criteria for this review fix.
