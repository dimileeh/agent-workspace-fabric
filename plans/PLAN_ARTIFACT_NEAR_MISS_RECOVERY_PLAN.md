# AWF Planning Artifact Near-Miss Recovery Plan

## Summary
Recover a narrowly safe planning mistake where an agent writes exactly one ignored
`docs/awf-plans/ws_*.md` plan artifact with a near-miss workspace id instead of
the required `docs/awf-plans/{workspace_id}.md` path.

## Approach
- Keep the existing exact-path requirement and digest fallback for correct
  ignored plan files.
- Snapshot direct `docs/awf-plans/ws_*.md` candidate file digests before and
  after the planning agent run.
- If the required plan file is still missing, recover only when there is exactly
  one new or changed sibling markdown file whose filename has the same length as
  the required filename and Hamming distance at most two.
- Require no tracked, staged, committed, or source-path changes during planning;
  unsafe or ambiguous cases continue to fail with
  `AGENT_PLAN_PHASE_SCOPE_VIOLATION`.
- Move the typo artifact to the required path and remove the near-miss path, then
  continue through normal implementation and conformance.

## Tests
- Single ignored near-miss plan artifact is moved to the required path and the
  workspace continues.
- Multiple near-miss artifacts fail without guessing and include evidence.
- Near-miss plus source changes fails as a planning scope violation.
- Non-near filenames, nested files, JSON artifacts, and `README.md` are ignored.
- Existing required plan files are not overwritten.
- Correct ignored plan artifact behavior remains unchanged.
