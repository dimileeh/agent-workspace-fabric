# Plan: PRRT_kwDOSJAM6s6FjBWJ Null-Node Recovery

## Scope

Address the review thread about named workers counting legacy `NULL` `Workspace.node_id`
active rows for requested-admission capacity while stale-active recovery only scans
the exact worker node.

## Steps

1. Add a focused regression proving a named worker's stale-active recovery scan
   includes a legacy `NULL`-node active row that can consume admission slots.
2. Update the stale-active recovery candidate query so its node scope matches
   requested-admission's legacy `NULL`-row accounting.
3. Run the targeted regression test only; AWF/GitHub owns broad validation after
   agent completion.
4. Record validation evidence and commit the thread-specific fix locally.
