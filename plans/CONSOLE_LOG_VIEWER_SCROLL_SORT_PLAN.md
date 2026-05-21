# Console Log Viewer Scroll And Sort Plan

## Goal

Fix the AWF console log viewer so it behaves like an inspectable multi-stream
log browser instead of constantly tailing and reordering confusingly.

## Scope

- Make log sort ascending by default for inline and fullscreen logs.
- Preserve user scroll position when log content refreshes unless the user is
  already at the tail, changes sort direction, or explicitly tails.
- Keep the explicit Tail / Tail all controls as commands that jump back to the
  current tail.
- Order combined tail chunks by inferred stream activity, not just stream
  opened time:
  - live SSE log frames keep their event timestamp;
  - tail chunks use stream byte/line-count change time when a stream grows;
  - unchanged streams retain their previous activity time.
- Add focused Playwright coverage for default sort, scroll preservation, and
  multi-stream activity ordering.

## Validation

- Run focused console Playwright tests for the log viewer.
- Run console lint, typecheck, and build.
