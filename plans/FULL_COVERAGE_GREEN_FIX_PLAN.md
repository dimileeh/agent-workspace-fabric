# Full Coverage Green Fix Plan

## Problem

The full `pytest -n 20 --cov=awf --cov-fail-under=99` gate reaches 99% coverage
but fails on six tests:

- Five plain-file credential tests create trusted temporary anchor directories
  with default `Path.mkdir()`. On this Linux host the process umask is `0002`,
  producing group-writable `0775` anchors. The hardened credential writer
  correctly refuses group-writable ancestors before writing secrets, so the tests
  fail before exercising the intended race/durability behavior.
- One scheduler persistence test expects unknown capacity because it monkeypatches
  `workspaces_create.get_settings`, but `WorkspaceService.create()` now uses the
  service settings path that resolves local capacity. The persisted resource
  summary therefore contains detected limits instead of unknown-capacity reason
  codes.

## Fix

1. Update the affected credential tests to create their synthetic trusted
   anchors with explicit owner-private permissions (`mode=0o700`) so they are
   independent of ambient umask while preserving the security behavior under
   test.
2. Update the scheduler persistence test to call `workspaces.create_workspace_row`
   with explicit `Settings(local_capacity_* = None)` so the test genuinely
   exercises the unknown-capacity persistence contract it asserts.

## Validation

Run the focused failing tests first, then the full `-n 20` coverage command.
