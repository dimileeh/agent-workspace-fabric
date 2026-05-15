# Security Cleanup Audit Plan

## Problem Statement and Scope

Implement the narrow P1 low-risk security cleanup slice from the 2026-05-14
architecture/security review, using
`docs/awf-plans/ws_7bad4fd57a2b4995acc9292a.md` as the AWF implementation
contract.

This plan covers only:

- replacing fragile SQL interval string interpolation in PostgreSQL scheduler
  scoring;
- reducing selected workspace create/idempotency/conflict 409 payload leakage
  of internal names such as `task_external_id`;
- proving doctor/support-bundle known-secret sets are redaction inputs only.

Out of scope: API auth posture, callback SSRF, admission/rate limiting,
production config guardrails, broad scheduler/service refactors, generated
artifacts, CI workflows, dependency lockfiles, and full local coverage.

## Requirements Checklist

- [ ] Add failing regression tests before implementation for still-valid
  findings.
- [ ] Remove `text(f"INTERVAL ... seconds")` from
  `src/awf/db/repositories.py` while preserving PostgreSQL scheduler ordering
  semantics.
- [ ] Add a static regression check preventing the fragile interval pattern from
  returning to the scoring helper.
- [ ] Ensure selected 409 payloads keep stable error codes and actionable
  public fields while avoiding `task_external_id` in serialized responses.
- [ ] Add doctor/support-bundle sentinel tests proving configured API tokens and
  database credentials are redacted from emitted outputs.
- [ ] Keep implementation changes scoped to the owned surface.
- [ ] Create a validation report after implementation.

## Implementation Steps

1. Inspect the existing repository, API, doctor, and support-bundle tests around
   the targeted behavior.
2. Update tests first:
   - PostgreSQL scheduler SQL should use a SQLAlchemy-built interval expression
     and not raw `INTERVAL '... seconds'` text.
   - API conflict/idempotency responses should not serialize
     `task_external_id`.
   - Doctor/support-bundle outputs should not emit sentinel API tokens or
     database passwords.
3. Run the focused test slices to confirm failures for still-valid findings and
   note any proof tests that already pass.
4. Implement the smallest source changes needed:
   - Replace the PostgreSQL interval text literal with a typed/parameterized
     SQLAlchemy expression.
   - Narrowly adjust workspace 409 wording only if required by the tests.
   - Fix redaction narrowly only if the sentinel tests reveal a leak.
5. Re-run focused tests and static searches.
6. Run ruff and mypy.
7. Write `plans/SECURITY_CLEANUP_AUDIT_VALIDATION.md` with requirement status
   and evidence.

## Assumptions/Changes

- A final source scan found the same task external ID conflict payload duplicated
  in the MCP workspace-create tool. Treating that as the same selected
  create/conflict operator surface keeps REST and MCP payload wording aligned
  without expanding into unrelated auth, callback, rate-limit, or config work.

## Verification Commands and Pass Criteria

Focused validation:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/db/test_workspace_repository.py tests/unit/api/test_workspaces.py tests/unit/api/test_route_error_edges.py tests/unit/service/test_doctor.py tests/unit/service/test_support_bundle.py -q
uv run --python 3.12 --extra dev ruff check src/awf tests
uv run --python 3.12 --extra dev mypy src/awf
```

Static checks:

```bash
rg 'text\(f"INTERVAL|INTERVAL '\''[0-9]+ seconds'\''' src/awf/db/repositories.py tests/unit/db/test_workspace_repository.py
rg 'task_external_id' src/awf/api/routes/workspaces.py tests/unit/api/test_workspaces.py tests/unit/api/test_route_error_edges.py
```

Pass criteria:

- No fragile SQL interval string interpolation remains in scheduler scoring.
- Scheduler SQL and ordering behavior remain covered.
- Selected 409/error payloads avoid `task_external_id` while preserving stable
  error codes and actionable public guidance.
- Doctor/support-bundle emitted JSON, pretty output, and written artifacts do
  not include sentinel configured secrets.
- Focused pytest, ruff, and mypy commands pass or any environment blocker is
  documented in validation.
