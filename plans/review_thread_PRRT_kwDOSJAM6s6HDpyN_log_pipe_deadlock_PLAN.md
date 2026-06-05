# Review Thread PRRT_kwDOSJAM6s6HDpyN Log Pipe Deadlock Plan

## Problem Statement and Scope

The review reports that `awf service logs --follow` can deadlock when output is
piped to a downstream command that exits early. The current follow runner uses
`Popen` pipes and reader threads. If a reader thread hits `BrokenPipeError` while
writing to `sys.stdout` or `sys.stderr`, the child process can continue writing
into a pipe whose read end remains open, and the main thread can block forever in
`process.wait()`.

Scope is limited to the service log streaming subprocess helper and a focused
unit regression.

## Requirements Checklist

- Verify the review claim against `src/awf/service/logs.py`.
- Add a regression test showing a broken streaming sink causes the followed
  subprocess to be terminated and reaped instead of waiting indefinitely.
- Preserve existing redaction and keyboard-interrupt behavior.
- Avoid broad validation; run only targeted unit tests for the touched behavior.
- Commit the focused fix locally without pushing.

## Implementation Steps

1. Add a failing unit test in `tests/unit/service/test_logs_parts/test_logs_part_002.py`
   that simulates a followed process whose output sink raises `BrokenPipeError`.
2. Update `src/awf/service/logs.py` so stream-thread sink failures are reported to
   the main follow runner.
3. When a stream thread reports a broken downstream pipe, terminate and reap the
   child process before returning.
4. Keep normal successful streaming and keyboard-interrupt cleanup semantics
   unchanged.

## Verification Commands and Pass Criteria

- Run the new focused test first and confirm it fails before the implementation.
- After implementation, run:
  `uv run --python 3.12 --extra dev pytest tests/unit/service/test_logs_parts/test_logs_part_002.py -q`
- Pass criteria: the targeted service log unit tests pass. Full AWF/GitHub
  validation is managed by AWF after agent completion.
