# Companion Environment Interpolation Plan

## Problem Statement And Scope

Inline review thread `PRRT_kwDOSJAM6s6FOpgS` reports that public companion
environment values can include Docker Compose interpolation syntax such as
`${GITHUB_TOKEN}`. Those values are rendered into a Compose environment block,
where Compose can substitute values from the AWF/Compose process environment.

Scope is limited to public companion request validation and focused regression
coverage for companion environment strings.

## Requirements

- [ ] Reject companion-supplied environment values containing Docker Compose
      interpolation syntax.
- [ ] Preserve ordinary literal companion environment values, including values
      with non-interpolation dollar characters.
- [ ] Add a regression test proving `${GITHUB_TOKEN}` is rejected by the public
      workspace create contract.
- [ ] Keep validation focused; broad AWF/GitHub validation remains managed by
      AWF after agent completion.

## Implementation Steps

1. Add a failing public schema regression to
   `tests/unit/api/test_schema_coverage_edges.py`.
2. Add companion environment value validation in
   `src/awf/api/schemas_companions.py`.
3. Run focused tests for the touched companion schema behavior.
4. Run focused lint for the touched files.

## Verification Commands

- `uv run --python 3.12 --extra dev pytest tests/unit/api/test_schema_coverage_edges.py::test_workspace_companions_reject_invalid_public_contract -q`
- `uv run --python 3.12 --extra dev pytest tests/unit/api/test_schema_coverage_edges.py -q -k "companion"`
- `uv run --python 3.12 --extra dev ruff check src/awf/api/schemas_companions.py tests/unit/api/test_schema_coverage_edges.py`

Full repository validation and CI-equivalent gates are intentionally not run in
the agent phase per the AWF workspace contract.
