# PRRT_kwDOSJAM6s6DbteD Prerelease Labels Plan

## Problem Statement and Scope

Review thread `PRRT_kwDOSJAM6s6DbteD` reports that protected workflow action version comparisons treat alphanumeric prerelease labels such as `rc10` as numeric chunks. SemVer compares non-numeric prerelease identifiers lexically as whole identifiers, so `rc10` has lower precedence than `rc2`.

Scope is limited to the local workflow `uses` pinned version comparator and its unit regression coverage.

## Requirements Checklist

- Add a regression test proving `v1.0.0-rc2` to `v1.0.0-rc10` is blocked as a downgrade.
- Preserve existing prerelease downgrade protections.
- Compare purely numeric prerelease identifiers numerically.
- Compare non-numeric prerelease identifiers lexically as whole identifiers.
- Keep changes scoped to `quality_gates` and direct tests.

## Implementation Steps

1. Update the prerelease downgrade parameterized test to include the reported `rc2 -> rc10` case.
2. Replace chunk-based non-numeric prerelease sort keys with whole-identifier lexical keys.
3. Run the targeted unit test first, then the relevant quality-gate unit surface if practical.

## Assumptions/Changes

- Existing regression coverage already required `rc10 -> rc2` to be blocked. The implementation therefore uses SemVer lexical keys to catch the reported downgrade, while preserving the existing conservative fail-closed behavior for same-core prerelease changes containing mixed alphanumeric identifiers such as `rc10`.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_quality_gates.py -k workflow_pinned_uses_prerelease_downgrade_is_blocked -q`
  - Passes and includes the new regression.
- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_quality_gates.py -q`
  - Passes for the touched quality-gate surface.
