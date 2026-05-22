# Console Log Viewer Scroll And Sort Validation

## Result

Implemented the planned console log viewer fixes:

- Log sort direction now defaults to ascending.
- Log output preserves the operator's manual scroll position on live refreshes unless the operator is already following the tail, changes sort direction, or explicitly presses Tail/Tail all.
- Combined tail entries use inferred stream activity from changing stream byte/line counts instead of only stream open/close timestamps.
- Added focused browser coverage for the default order, activity-based combined stream ordering, and scroll preservation.

## Validation

- `npm --prefix apps/console run lint` passed.
- `npm --prefix apps/console run typecheck` passed.
- `npm --prefix apps/console run build` passed.
- Focused Playwright validation passed against the live console on port 3000 with a no-webserver config:
  - `dashboard-log-viewer.spec.ts`

## Notes

The standard Playwright webServer path could not start a second Next dev server while the operator console was already running. The focused browser test was therefore run against the existing console process with route-level API mocks.
