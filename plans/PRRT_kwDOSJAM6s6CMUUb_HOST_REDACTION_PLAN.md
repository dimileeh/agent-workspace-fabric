# PRRT_kwDOSJAM6s6CMUUb Host Redaction Plan

## Problem Statement and Scope

The setup dependency network classifier extracts `host` from raw command and
output context. A review thread reports that the fallback dotted-host extractor
can treat a dotted secret, such as a JWT-like environment value before any real
host, as `classification.host`. Retry and exhaustion logs then emit that raw
host value.

Scope is limited to setup dependency network host extraction and regression
coverage in `tests/unit/runtime/test_validation.py`.

## Requirements Checklist

- Add a regression test that fails when a JWT-like dotted secret is extracted as
  setup dependency `host`.
- Preserve valid URL host extraction, including URLs with credentials in the
  userinfo section.
- Preserve existing fallback-host behavior for safe, non-secret dotted hostnames.
- Reject host candidates that would be changed by `redact_audit_text`.
- Run targeted validation for the changed runtime validation behavior.

## Implementation Steps

1. Add a unit test for a setup dependency DNS failure where `TOKEN=<jwt>` appears
   before any actual hostname and the diagnostic contains no host.
2. Confirm the new test fails against the current extractor.
3. Add a small host safety helper mirroring the package safety check.
4. Apply that helper to URL-derived and fallback-derived host candidates.
5. Run the targeted test module or focused tests.

## Verification Commands and Pass Criteria

```bash
uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_validation.py -q
```

Pass criteria: the new regression and existing validation runtime tests pass,
and no setup dependency metadata/log path can receive a redacted JWT-like value
as `host`.
