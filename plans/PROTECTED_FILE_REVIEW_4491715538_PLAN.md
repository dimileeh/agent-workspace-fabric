# Protected File Review 4491715538 Plan

## Problem Statement And Scope

Address the review-level feedback from PR comment `issue:4491715538` for the
diff-aware protected quality-gate classifier. Scope is limited to clearer
violation messaging/documentation and more robust protected-file content loading.

## Requirements Checklist

- Rephrase coverage `fail_under` raise violations so the reason clearly states
  ownership enforcement instead of implying a regression.
- Preserve the existing safety policy that new top-level `[dependency-groups]`
  entries are blocked without ownership, and document that asymmetry clearly.
- When changed dependency groups contain valid PEP 735 `{ include-group = ... }`
  entries that AWF cannot evaluate, report that limitation directly instead of
  calling the file format unsupported.
- Replace locale-dependent `git show` missing-path detection with a
  `git cat-file -e` existence precheck that treats missing paths as absent
  content while preserving fail-closed behavior for invalid refs and unexpected
  `git show` failures.
- Add or update focused regression tests before implementation where practical.
- Commit the final scoped changes locally without pushing or switching branches.

## Implementation Steps

1. Update focused unit tests for the desired coverage message, PEP 735
   include-group wording, and `git_show_text` cat-file precheck behavior.
2. Run the narrow affected tests and confirm the new assertions fail before the
   implementation change where practical.
3. Implement the message and loader changes in the smallest local scope.
4. Clarify `docs/PROTECTED_FILES.md` around existing vs. new
   `[dependency-groups]` entries.
5. Re-run the targeted tests, then run the narrow lint surface for touched
   Python files.
6. Write `plans/PROTECTED_FILE_REVIEW_4491715538_VALIDATION.md` with
   requirement-by-requirement evidence.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_quality_gates.py tests/unit/control/test_protected_file_diffs.py -q`
  passes.
- `uv run --python 3.12 --extra dev ruff check src/awf/control/quality_gates.py src/awf/control/protected_file_diffs.py tests/unit/control/test_quality_gates.py tests/unit/control/test_protected_file_diffs.py`
  passes.
