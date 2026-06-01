# Plan: verify_awf PATH fallback identity check

## Scope

Resolve duplicate issues #331 and #340 in `packaging/install.sh`: the default
install fallback in `verify_awf()` must not report success only because some
stale or unrelated `awf` is reachable on `PATH`.

## Steps

1. Add installer regression tests first:
   - default install, no binary in resolved bin dir, stale PATH `awf` with a
     mismatched version fails with `AWF_NOT_REACHABLE` and honest PATH advice.
   - default install, no binary in resolved bin dir, PATH `awf` reporting the
     just-installed version succeeds without PATH advice.
2. Extend the installer test harness only as needed so fake `awf` binaries can
   report realistic `--version` output while preserving existing `--help`
   success/failure behavior.
3. Track the resolved install version during manifest/artifact verification,
   preferring manifest version, then pinned `--version`, then wheel filename
   version when no manifest version exists.
4. Harden only the default-install `command -v awf` fallback in `verify_awf()`:
   accept the reachable candidate only when its reported version matches the
   resolved install version; otherwise fall through to the existing
   `AWF_NOT_REACHABLE` path and keep the existing runnability check.
5. Validate with focused installer tests first, then run the operator-requested
   local gate commands.

## Non-Goals

- Do not change explicit `--install-dir` missing-binary behavior.
- Do not remove the default PATH fallback.
- Do not branch, push, commit, or open a PR; AWF owns lifecycle after this run.
