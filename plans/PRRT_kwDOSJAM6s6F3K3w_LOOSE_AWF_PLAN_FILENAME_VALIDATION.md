# PRRT_kwDOSJAM6s6F3K3w Loose AWF Plan Filename Validation

Plan reference:
`plans/PRRT_kwDOSJAM6s6F3K3w_LOOSE_AWF_PLAN_FILENAME_PLAN.md`

## Requirement Status

| Requirement | Status | Evidence |
| --- | --- | --- |
| Real top-level documentation files under `docs/awf-plans/` with alphabetic `ws_` suffixes remain ordinary owned paths. | Complete | Added negative coverage for `ws_protocol.md`, `ws_protocol.json`, `ws_protocol.conformance.json`, and `ws_v2.md`; the classifier now rejects arbitrary alphabetic `ws_` suffixes. |
| Generated AWF workspace artifacts under `docs/awf-plans/` remain internal artifacts for `.md`, `.json`, and `.conformance.json`. | Complete | Existing numeric shorthand tests still pass; added 24-hex workspace id artifact examples for each supported suffix. |
| Existing literal artifact glob owned paths such as `docs/awf-plans/ws_*.json` remain internal artifacts. | Complete | Existing literal `ws_*` glob assertions still pass after tightening the regex. |
| Nearby real docs, nested files, and note-like filenames remain ordinary owned paths. | Complete | Existing README, nested path, and note-like filename assertions still pass. |

## Evidence

Files changed:

- `src/awf/common/owned_paths.py`
- `tests/unit/common/test_owned_paths.py`
- `plans/PRRT_kwDOSJAM6s6F3K3w_LOOSE_AWF_PLAN_FILENAME_PLAN.md`
- `plans/PRRT_kwDOSJAM6s6F3K3w_LOOSE_AWF_PLAN_FILENAME_VALIDATION.md`

Focused checks:

- Pre-implementation regression command:
  `uv run --python 3.12 --extra dev pytest tests/unit/common/test_owned_paths.py::test_real_docs_and_repo_plan_paths_are_not_internal_plan_artifacts tests/unit/common/test_owned_paths.py::test_interworkspace_owned_paths_filters_only_internal_plan_artifacts -q`
  - Result before implementation: failed as expected; `ws_protocol.*` and
    `ws_v2.md` were incorrectly classified as internal artifacts.
- Post-implementation regression command:
  `uv run --python 3.12 --extra dev pytest tests/unit/common/test_owned_paths.py::test_real_docs_and_repo_plan_paths_are_not_internal_plan_artifacts tests/unit/common/test_owned_paths.py::test_interworkspace_owned_paths_filters_only_internal_plan_artifacts -q`
  - Result: passed, 14 tests.
- `uv run --python 3.12 --extra dev pytest tests/unit/common/test_owned_paths.py -q`
  - Result: passed, 35 tests.
- `uv run --python 3.12 --extra dev ruff check src/awf/common/owned_paths.py tests/unit/common/test_owned_paths.py`
  - Result: passed.

Full AWF/GitHub validation was not run in the agent phase; AWF owns broad
validation, provenance, and merge gating after completion.
