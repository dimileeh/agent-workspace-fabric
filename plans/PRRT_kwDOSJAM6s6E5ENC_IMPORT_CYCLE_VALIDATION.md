# PRRT_kwDOSJAM6s6E5ENC Import Cycle Validation

Plan reference:
`plans/PRRT_kwDOSJAM6s6E5ENC_IMPORT_CYCLE_PLAN.md`

## Requirement Status

- Remove runtime module-scope imports from `github_client_adoption.py` back to
  `github_client.py`: Complete.
  `src/awf/common/github_client_adoption.py` now keeps only type-checking
  imports at module scope and imports runtime DTO/error classes inside the
  functions that need them.
- Preserve public behavior and structured `PullRequestMetadataError` details:
  Complete. Error construction paths are unchanged aside from localizing the
  class imports.
- Keep type annotations valid without reintroducing runtime imports: Complete.
  Type-only annotations remain behind `TYPE_CHECKING` and `from __future__
  import annotations`.
- Validate with focused split-import and adoption/GitHub client tests:
  Complete.
- Do not run AWF/GitHub-owned broad validation: Complete. Only targeted tests
  and focused lint/type checks were run; AWF/GitHub validation remains managed
  after agent completion.

## Evidence

- Pre-fix failure confirmed:
  `uv run --python 3.12 --extra dev pytest tests/unit/common/test_github_client_split_imports.py -q`
  failed because importing `awf.common.github_client_adoption` first raised an
  `ImportError` from the `github_client.py` -> `github_client_adoption.py`
  cycle.
- Post-fix:
  `uv run --python 3.12 --extra dev pytest tests/unit/common/test_github_client_split_imports.py -q`
  passed: `2 passed`.
- Post-fix:
  `uv run --python 3.12 --extra dev pytest tests/unit/common/test_github_client_adoption_edges.py tests/unit/common/test_github_client_parts/test_github_client_part_001.py::TestFetchPullRequestAdoptionMetadata tests/unit/common/test_github_client_parts/test_github_client_part_001.py::TestBranchOpenPullRequestResolver -q`
  passed: `21 passed`.
- Post-fix:
  `uv run --python 3.12 --extra dev ruff check src/awf/common/github_client_adoption.py tests/unit/common/test_github_client_split_imports.py`
  passed.
- Post-fix:
  `uv run --python 3.12 --extra dev mypy src/awf/common/github_client_adoption.py`
  passed.

## Gaps

None.
