# Plan Execution Protocol (Mandatory)

This protocol is required for any non-trivial implementation in this repository.

## 1. Plan First (Required)

Before coding, create a plan file in `plans/`:

- Naming: `plans/<TOPIC>_PLAN.md`
- Include:
  - Problem statement and scope
  - Explicit requirements checklist
  - Implementation steps
  - Verification commands and pass criteria

## 2. Execute Against the Saved Plan

During implementation:

- Use the plan file as the source of truth.
- Keep execution aligned to the requirements checklist.
- If scope changes, update the same plan file with an "Assumptions/Changes" section.

## 3. Validate Against Original Plan

After implementation, create a validation file in `plans/`:

- Naming: `plans/<TOPIC>_VALIDATION.md`
- Include:
  - Plan reference (`<TOPIC>_PLAN.md`)
  - Requirement-by-requirement status: `Complete`, `Partial`, or `Missing`
  - Evidence (files changed + tests/commands run)

## 4. Mandatory Iteration on Gaps

If any requirement is `Partial` or `Missing`:

- Add an explicit "Iteration N" section in the validation file.
- Implement the highest-impact remaining gap first.
- Re-run verification and update status until all planned requirements are satisfied, or document an explicit defer reason.

## 5. Completion Rule

Work is complete only when:

- The plan file exists in `plans/`
- A validation file exists in `plans/`
- Remaining gaps (if any) are clearly listed with rationale and next iteration target
