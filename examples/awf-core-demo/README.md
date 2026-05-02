# AWF Core Demo

This is the maintained golden-path demo project for the local AWF Core release
gate. It is intentionally small but DB-backed so onboarding, profile preview,
service validation, smoke workspace payload generation, PR monitor wiring, and
cleanup can be exercised against a realistic project shape.

Run the local preview from the repository root:

```bash
awf init examples/awf-core-demo --include-smoke-request
awf service readiness --format json
```

The demo is not an Aira application. It is a generic Python/Postgres fixture
that proves AWF Core can recognize a project, draft a restricted local profile,
and produce a smoke-workspace request without launching anything implicitly.
