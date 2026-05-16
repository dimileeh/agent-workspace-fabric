# Review Comment 4303731394 Workspace Control Route Tests Validation

Plan reference:
`plans/review_comment_4303731394_workspace_control_route_tests_PLAN.md`

## Requirement Status

- Add direct tests for malformed JSON and non-object JSON request bodies using
  the `INVALID_REQUEST` response shape: Complete.
- Exercise the empty-body parsing branch and prove valid empty payloads still
  proxy to AWF: Complete.
- Cover invalid `requested_tier` values and the unsupported action branch for
  `requested_tier`: Complete.
- Cover valid `requested_tier` values 1, 2, and 3 for `revalidate`: Complete.
- Cover non-integer, negative, and valid `workspace_version` behavior:
  Complete.
- Verify body `idempotency_key` takes precedence over the `Idempotency-Key`
  request header: Complete.
- Verify body `workspace_version` takes precedence over the `If-Match` request
  header, and that the header is used when the body value is absent: Complete.
- Keep changes minimal and limited to tests plus required plan/validation docs
  unless a real source bug is found: Complete, with one source import fix
  required because the existing Node ESM tests could not load `awf-server.ts`
  through `next/server`.

## Evidence

Files changed:

- `apps/console/lib/workspace-control-routes.test.mjs`
- `apps/console/lib/awf-server.ts`
- `plans/review_comment_4303731394_workspace_control_route_tests_PLAN.md`
- `plans/review_comment_4303731394_workspace_control_route_tests_VALIDATION.md`

Commands run:

```bash
npm --prefix apps/console exec -- node --test --disable-warning=MODULE_TYPELESS_PACKAGE_JSON lib/workspace-control-routes.test.mjs
node --test --disable-warning=MODULE_TYPELESS_PACKAGE_JSON lib/workspace-control-routes.test.mjs
node -e "import('next/server.js').then(() => console.log('next/server.js ok')).catch((err) => { console.error(err); process.exit(1); })"
node -e "import('next/server').then(() => console.log('next/server ok')).catch((err) => { console.error(err.code + ': ' + err.message); process.exit(1); })"
node --test --disable-warning=MODULE_TYPELESS_PACKAGE_JSON lib/workspace-control-routes.test.mjs
npm --prefix apps/console run test
npm --prefix apps/console run lint
npm --prefix apps/console run typecheck
```

Results:

- The initial `npm exec` focused-test command failed because it ran from the
  wrong working directory and could not find `lib/workspace-control-routes.test.mjs`.
- The first direct focused-test run failed before executing assertions because
  `next/server` could not be resolved by Node's ESM loader from the installed
  Next package.
- `import('next/server.js')` succeeded; `import('next/server')` failed with
  `ERR_MODULE_NOT_FOUND`. The review summary's import recommendation is
  therefore a false-positive sub-item for this test runtime.
- After restoring `next/server.js`, the focused route test passed with 13
  tests.
- The full console unit test script passed with 97 tests.
- Console lint passed.
- Console typecheck passed.
