# PRRT_kwDOSJAM6s6Djswo Literal Pathspec Plan

## Problem Statement And Scope

PR #268 review thread `PRRT_kwDOSJAM6s6Djswo` reports that the protected-file
diff missing-path recovery probe passes refspec paths directly to `git ls-files`
and `git ls-tree` as pathspecs. Paths containing Git pathspec metacharacters can
match other files and make the recovery check classify the exact protected path
incorrectly.

Scope is limited to the shared protected-file diff loader and focused unit
coverage for missing-path recovery probes.

## Requirements Checklist

- Treat recovered refspec paths as literal Git paths for both index and tree
  missing-path probes.
- Preserve the existing exact returned-path comparison used to distinguish
  corrupt/infrastructure failures from genuinely missing paths.
- Add regression coverage for metacharacter paths on both `HEAD:path` and
  `:path` refspec forms.
- Run the narrow unit tests for protected-file diff loading.
- Do not switch branches, push, or alter unrelated files.

## Implementation Steps

1. Add failing unit assertions that missing-path recovery invokes Git with
   literal pathspec magic for paths containing metacharacters.
2. Add a small helper in `src/awf/control/protected_file_diffs.py` to render
   literal pathspecs and use it in both recovery probes.
3. Re-run the targeted unit tests.
4. Record validation evidence in the matching validation file.

## Verification Commands And Pass Criteria

```bash
uv run --python 3.12 --extra dev pytest tests/unit/control/test_protected_file_diffs.py -q
```

Pass criteria: the targeted test module passes and the changed assertions show
that metacharacter paths are passed to Git using literal pathspec magic.
