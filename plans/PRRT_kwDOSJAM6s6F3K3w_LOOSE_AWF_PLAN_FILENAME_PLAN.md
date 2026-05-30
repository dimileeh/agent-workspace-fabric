# PRRT_kwDOSJAM6s6F3K3w Loose AWF Plan Filename Plan

## Problem Statement And Scope

The default internal plan artifact classifier for `docs/awf-plans/` treats any
top-level `ws_*.md` or `ws_*.json`-shaped filename as generated AWF metadata.
That can hide real repository docs such as `docs/awf-plans/ws_protocol.md` from
inter-workspace overlap, lock, merge-queue, and staleness checks.

Scope is limited to `src/awf/common/owned_paths.py` and focused unit coverage in
`tests/unit/common/test_owned_paths.py`.

## Requirements Checklist

- Real top-level documentation files under `docs/awf-plans/` with alphabetic
  `ws_` suffixes remain ordinary owned paths.
- Generated AWF workspace artifacts under `docs/awf-plans/` remain internal
  artifacts for `.md`, `.json`, and `.conformance.json`.
- Existing literal artifact glob owned paths such as `docs/awf-plans/ws_*.json`
  remain internal artifacts.
- Nearby real docs, nested files, and note-like filenames remain ordinary owned
  paths.

## Implementation Steps

1. Add focused failing regression coverage for `docs/awf-plans/ws_protocol.md`
   and related alphabetic `ws_` filenames.
2. Run the focused owned-path regression to confirm the current classifier is
   too broad.
3. Tighten the default `docs/awf-plans/` filename classifier so it accepts
   generated workspace ids and literal generated globs, not arbitrary `ws_`
   filenames.
4. Re-run focused tests and lint for the touched helper and unit test.

## Verification Commands

- `uv run --python 3.12 --extra dev pytest tests/unit/common/test_owned_paths.py -q`
- `uv run --python 3.12 --extra dev ruff check src/awf/common/owned_paths.py tests/unit/common/test_owned_paths.py`
- Full AWF/GitHub validation is intentionally not run in the agent phase; AWF
  owns broad validation, provenance, and merge gating after completion.
