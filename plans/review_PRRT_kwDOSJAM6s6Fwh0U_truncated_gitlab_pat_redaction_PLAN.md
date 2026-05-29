# Review PRRT_kwDOSJAM6s6Fwh0U Truncated GitLab PAT Redaction Plan

## Problem Statement And Scope

Inline review thread `PRRT_kwDOSJAM6s6Fwh0U` reports that shortened rejected
GitLab PAT-looking values such as `glpat-a` are accepted by config secret
detection as secret-like input but are not redacted by the shared token pattern
used by first-run rendering, log redaction, and audit redaction. The scope is
limited to aligning shared GitLab PAT redaction with the existing
`glpat-` prefix rejection behavior.

## Requirements Checklist

- Add regression coverage proving truncated `glpat-` values are redacted by the
  shared redactors.
- Preserve shared pattern usage across audit, log, and first-run renderers.
- Keep the code change scoped to the GitLab PAT threshold in the shared token
  pattern.
- Run only focused checks for the changed behavior; full AWF/GitHub validation
  remains managed by AWF after agent completion.

## Implementation Steps

1. Add failing unit coverage for truncated GitLab PAT redaction.
2. Update `KNOWN_TOKEN_PATTERN` to redact any non-empty GitLab PAT suffix after
   `glpat-`.
3. Run the focused tests that cover shared token patterns, log redaction, and
   first-run rendering.
4. Write validation evidence in the matching validation document.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_log_redaction.py tests/unit/service/test_host_setup_rendering.py tests/unit/common/test_token_patterns.py -q`
  - Passes with the new truncated GitLab PAT regression coverage.

Full AWF/GitHub validation is intentionally not run in the agent phase per the
workspace contract.
