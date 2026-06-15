# PRRT_kwDOSJAM6s6JuSPr Duplicate Lookup Plan

## Scope
Fix PR creator duplicate-PR reconciliation so a failed `gh pr list` lookup is not treated as a genuine empty result.

## Steps
1. Add a focused regression test for duplicate `gh pr create` plus failed reconciliation lookup.
2. Update `src/awf/runtime/pr_creator.py` to distinguish failed lookup details from not-found lookup details on duplicate errors.
3. Run the narrow affected test(s) only; AWF/GitHub own broad validation after this agent phase.
4. Record validation outcome in `plans/PRRT_kwDOSJAM6s6JuSPr_VALIDATION.md`.
