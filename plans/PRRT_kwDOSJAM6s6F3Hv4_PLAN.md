# PRRT_kwDOSJAM6s6F3Hv4 Plan

## Problem Statement and Scope

The review thread reports that requested owned paths are filtered with
profile-configured internal planning artifact templates without passing a known
workspace id. For retry flows, the source workspace id is already known, so
custom templates such as `docs/{workspace_id}.md` should filter only that
workspace's generated artifact path instead of using the unknown-id `ws_*`
fallback.

Scope is limited to owned-path overlap lookup behavior and the retry caller
that already has a workspace id.

## Requirements Checklist

- Add a regression test proving a known requested workspace id preserves real
  requested paths that would match the unknown-id artifact fallback.
- Allow `WorkspaceRepository.find_active_owned_path_overlaps` and the legacy
  conflict wrapper to pass a known requested workspace id into internal artifact
  filtering.
- Pass the source workspace id from retry overlap detection.
- Preserve existing fresh-create behavior where no workspace id exists yet.
- Run only focused tests or checks for the changed behavior; broad AWF/GitHub
  validation remains managed after the agent phase.

## Implementation Steps

1. Add a focused repository test under the owned-path overlap lookup tests.
2. Confirm the new regression fails against the current implementation when
   practical.
3. Add an optional requested workspace id argument to the repository overlap
   methods and use it for requested path filtering.
4. Update the retry service caller to pass `source.id`.
5. Re-run the focused regression and nearby repository overlap tests as needed.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/db/test_workspace_repository_parts/test_workspace_repository_part_002.py::TestOwnedPathOverlapLookup::test_known_requested_workspace_id_does_not_filter_other_ws_shaped_docs_path -q`
  passes.
- A focused subset of owned-path repository overlap tests passes.
- Full AWF/GitHub validation is not run locally; AWF owns broad validation,
  provenance, logs, and merge gating after completion.
