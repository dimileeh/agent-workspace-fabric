# Authorization Header OpenAPI Plan

## Problem Statement and Scope

PR review thread `PRRT_kwDOSJAM6s6CNJ4D` reports that required `authorization`
header parameters in `openapi.json` are still generated as nullable schemas.
The fix should update the generated OpenAPI contract for auth-required routes so
the checked-in artifact and generator stay aligned.

## Requirements Checklist

- Add a regression test proving every required `authorization` header schema is
  a non-null string.
- Update OpenAPI generation so auth-required `authorization` headers are marked
  required and documented as `type: string` with `minLength: 1`.
- Regenerate `openapi.json` from the app rather than hand-editing the artifact.
- Validate with the narrow OpenAPI tests and the OpenAPI drift check.
- Commit only the files changed for this thread with a conventional commit
  referencing `PRRT_kwDOSJAM6s6CNJ4D`.

## Implementation Steps

1. Extend `tests/unit/api/test_openapi_artifact.py` with an assertion that no
   required `authorization` header keeps nullable `anyOf` schema output.
2. Run the focused test and confirm it fails against the current code.
3. Update `src/awf/api/app.py` OpenAPI post-processing to normalize the
   required authorization header schema.
4. Regenerate `openapi.json` using `scripts/generate_openapi.py`.
5. Run focused tests plus `python scripts/generate_openapi.py --check`.
6. Write `plans/AUTH_HEADER_OPENAPI_VALIDATION.md` with requirement evidence.
7. Stage the touched files and commit locally.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/api/test_openapi_artifact.py -q`
  passes.
- `python scripts/generate_openapi.py --check` reports the checked-in artifact
  matches the generated spec.
