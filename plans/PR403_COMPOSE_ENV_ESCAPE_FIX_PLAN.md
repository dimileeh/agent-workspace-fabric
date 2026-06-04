# PR403 Compose Env Escape Fix Plan

## Problem

A PR review comment found that the host-side Compose env parser consumes
backslashes inside double-quoted values before the double-quote decoder sees
them. That makes values such as `\n`, `\$`, `\"`, and `\\` diverge from the
values Docker Compose applies to containers.

## Plan

- Add a regression test for escaped double-quoted Compose env values.
- Preserve escape markers while consuming double-quoted values, so
  `_decode_compose_double_quoted_value()` performs the actual decoding.
- Keep single-quoted values literal and existing interpolation behavior intact.
- Validate the focused env parser tests, ruff/format, mypy, and local shard 2
  after the fix.
