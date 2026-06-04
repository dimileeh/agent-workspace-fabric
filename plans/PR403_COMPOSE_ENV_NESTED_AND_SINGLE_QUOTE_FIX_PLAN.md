# PR403 Compose Env Nested And Single Quote Fix Plan

## Problem

Two new PR #403 review comments found Compose env-file divergences:

- Single-quoted env values can escape a single quote with `\'`; the host parser
  currently stops at the escaped quote and truncates the value.
- Nested Compose interpolation such as `${CUSTOM_DIR:-${HOME}/.awf/service}` is
  supported by Compose but the host parser stops the outer expression at the
  first `}`.

## Plan

- Add regressions for escaped single quotes in single-quoted env values and
  nested default interpolation.
- Update the single-quoted value consumer to honor escaped quotes while keeping
  single-quoted values uninterpolated.
- Replace the regex-only env-value interpolation pass with a small scanner that
  can find matching nested `${...}` braces and reuse the existing operator
  semantics.
- Validate focused environment tests plus lint/format/mypy.
