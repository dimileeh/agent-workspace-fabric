# PRRT_kwDOSJAM6s6DbteD Prerelease Labels Validation

Plan reference: `PRRT_kwDOSJAM6s6DbteD_PRERELEASE_LABELS_PLAN.md`

## Requirement Status

- Complete: Added a regression test proving `v1.0.0-rc2` to `v1.0.0-rc10` is blocked as a downgrade.
- Complete: Preserved existing prerelease downgrade protections, including the existing `rc10 -> rc2` fail-closed regression.
- Complete: Purely numeric prerelease identifiers continue to compare numerically.
- Complete: Non-numeric prerelease identifiers now compare lexically as whole identifiers.
- Complete: Changes are scoped to `src/awf/control/quality_gates.py`, `tests/unit/control/test_quality_gates.py`, and the required plan/validation artifacts.

## Evidence

- Updated `tests/unit/control/test_quality_gates.py` with the reported `rc2 -> rc10` regression.
- Updated `src/awf/control/quality_gates.py` to remove chunk-based non-numeric prerelease keys and preserve fail-closed handling for same-core mixed alphanumeric prerelease label changes.
- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_quality_gates.py -k workflow_pinned_uses_prerelease_downgrade_is_blocked -q` passed.
- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_quality_gates.py -q` passed.
- `uv run --python 3.12 --extra dev ruff check src/awf/control/quality_gates.py tests/unit/control/test_quality_gates.py` passed.
- `uv run --python 3.12 --extra dev ruff format --check src/awf/control/quality_gates.py` passed.
- `uv run --python 3.12 --extra dev mypy src/awf` passed.

## Gaps

- None.
