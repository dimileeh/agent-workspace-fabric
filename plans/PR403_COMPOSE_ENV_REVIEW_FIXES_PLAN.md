# PR403 Compose Env Review Fixes Plan

## Problem

Three PR review comments found remaining host-side Compose env divergences:

- Legacy env migration can emit unsafe single-quoted values when a secret
  contains both `$` and `'`.
- Env-file interpolation gives previously parsed file values precedence over
  the caller environment, while Docker Compose gives the caller environment
  precedence.
- Mandatory interpolation operators (`:?` and `?`) silently resolve to empty
  strings host-side even though Docker Compose fails startup.

## Plan

- Add focused regression tests for each review issue before changing behavior.
- Format migrated dollar-containing values with Compose-safe quoting:
  single-quote literal dollar values only when they contain no single quote,
  otherwise double-quote them and escape dollars to keep them literal.
- Reverse env-file interpolation context precedence so caller environment wins
  over previously parsed env-file values.
- Add a distinct Compose env interpolation exception for failed mandatory
  operators and raise it when `:?` or `?` conditions are not satisfied.
- Validate with focused env tests, ruff/format, mypy, and the relevant local
  service unit shard.
