# Review Thread PRRT_kwDOSJAM6s6CjUpm Plan

## Problem Statement and Scope

The console BFF proxy in `apps/console/lib/awf-server.ts` forwards requests to
the AWF API with `fetch` but does not bound the request duration. A hung backend
can therefore leave the route pending indefinitely.

Scope is limited to adding a timeout to the AWF API proxy fetch path and focused
console regression coverage.

## Requirements Checklist

- Preserve existing proxy behavior for successful responses, headers, methods,
  and request bodies.
- Abort AWF API proxy fetches after a bounded timeout.
- Keep timeout failures mapped through the existing `AWF_API_UNREACHABLE` 502
  response path.
- Add focused Node test coverage proving the proxy supplies and honors an abort
  signal.

## Implementation Steps

1. Add a focused console test that configures a short timeout and proves a hung
   backend fetch is aborted.
2. Confirm the focused regression fails against the current implementation.
3. Implement an `AbortController` timeout around the `proxyAwf` fetch call.
4. Run the focused test and console validation commands needed for the touched
   area.

## Verification Commands and Pass Criteria

- `node --test --disable-warning=MODULE_TYPELESS_PACKAGE_JSON
  lib/awf-server.test.mjs` from `apps/console` fails before implementation and
  passes after implementation.
- `npm --prefix apps/console test` passes.
- `npm --prefix apps/console run lint` passes.
- `npm --prefix apps/console run typecheck` passes.
- `npm --prefix apps/console run build` passes.
