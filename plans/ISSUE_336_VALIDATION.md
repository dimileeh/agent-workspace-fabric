# ISSUE-336 Validation

## Changes made
1. `src/awf/runtime/pr_monitor_runner/constants.py` — Added 4 DNS-resolution markers to `_TRANSIENT_GITHUB_ERROR_MARKERS`:
   - `could not resolve host`
   - `temporary failure in name resolution`
   - `name or service not known`
   - `could not resolve proxy`

2. `tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_001.py` — Added `test_dns_base_fetch_errors_classified_transient` covering:
   - All 4 DNS markers classified transient (True)
   - `could not resolve to a repository` still non-transient (False)
   - `could not resolve to a node` still non-transient (False)

## Acceptance criteria verified
- DNS resolution errors (`could not resolve host`, `temporary failure in name resolution`, `name or service not known`, `could not resolve proxy`) now classified transient via existing retry path — PASS
- Existing non-transient classifications (`could not resolve to a repository`, `could not resolve to a node`) unchanged — PASS
- Regression tests cover both DNS-transient and non-transient non-regression — PASS
- No new retry path or budget changes — confirmed, only marker set extended — PASS
- ruff check: All checks passed
- mypy: Success: no issues in 2 source files
- pytest (coverage edges): 195 passed

## Non-collision proof
`_NON_TRANSIENT_GITHUB_ERROR_MARKERS` contains `could not resolve to a repository` and `could not resolve to a node`. The new transient markers (`could not resolve host`, etc.) are distinct strings. The classifier checks non-transient FIRST, so a `could not resolve to a repository` error matches non-transient before any transient check. The test explicitly asserts this.

## What AWF/GitHub CI will verify
Full coverage gate, openapi spec drift, broader test suite.
