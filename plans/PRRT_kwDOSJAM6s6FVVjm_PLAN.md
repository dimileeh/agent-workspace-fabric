# PRRT_kwDOSJAM6s6FVVjm Plan

## Problem Statement and Scope

PR review thread `PRRT_kwDOSJAM6s6FVVjm` reports that companion
`environment_secrets` metadata is only persisted through secret-lease mount
metadata. When a workspace has companion env secrets but the profile declares no
secret leases, `mark_issued_mounted` has no issued leases to update, so the
companion provenance and optional omission counts are lost after provisioning.

Scope is limited to provisioning persistence for non-secret companion env secret
metadata and focused regression coverage.

## Requirements Checklist

- Add regression coverage for a workspace with companion env secret metadata and
  no profile-declared secret leases.
- Persist companion env secret provenance and optional omission counts through a
  path that does not depend on profile secret leases.
- Preserve existing profile secret-lease mount metadata behavior.
- Avoid storing raw secret values or unrelated stack metadata in the new durable
  record.
- Run only targeted checks; broad AWF/GitHub validation remains owned by AWF
  after agent completion.

## Implementation Steps

1. Add a failing provisioner unit test for companion env secret metadata with no
   profile leases.
2. Add a provisioner helper that builds an allowlisted companion env secret
   metadata event payload from stack launch metadata.
3. Record a workspace event after successful stack launch whenever companion env
   secret metadata is present.
4. Re-run the targeted test and a focused provisioner test subset.
5. Document validation results in
   `plans/PRRT_kwDOSJAM6s6FVVjm_VALIDATION.md`.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/node/test_provisioner_parts/test_provisioner_part_001.py -k "companion_secret_metadata_event_persists_without_profile_secret_leases" -q`
  - Passes after implementation and fails before implementation.
- `uv run --python 3.12 --extra dev pytest tests/unit/node/test_provisioner_parts/test_provisioner_part_001.py -k "secret_lease_mount_metadata or companion_secret_metadata_event" -q`
  - Passes, proving the new event behavior and existing lease mount metadata
    behavior remain compatible.
