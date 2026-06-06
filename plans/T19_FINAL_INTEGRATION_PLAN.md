# T19 Final Integration Plan

## Problem Statement And Scope

T19 is the final coordination task for the AWF full installer and first-run
setup backlog. All implementation dependencies are merged, so this task should
prove that the integrated `development` branch satisfies the backlog contract
without launching another AWF workspace.

This is a local validation and bookkeeping task. It should not change
production code unless a validation gate exposes a real bug.

## Requirements Checklist

- Confirm T19 dependencies are merged, including T21 PR #428.
- Run the integrated Python quality gates: ruff, mypy, unit tests, OpenAPI
  drift, and full coverage at or above 99%.
- Verify first-run CLI surfaces: `awf setup`, `awf start`, `awf init`, and
  `awf mcp serve`.
- Verify source-checkout setup can run as a read-only dry run.
- Verify first-run smoke lanes from outside the normal source-checkout path.
- Verify release package integration: build distributions, generate checksums
  and install manifest, run release artifact drift check, and run installer
  checksum smoke.
- Record validation evidence in `plans/T19_FINAL_INTEGRATION_VALIDATION.md`.
- Update `TODO/awf-full-installer-first-run-setup-backlog.md` to mark T19 done
  only after validation succeeds.
- Commit the T19 validation/bookkeeping result locally only; do not push.

## Execution Steps

1. Ensure the worktree starts on `development`, with only expected backlog
   bookkeeping changes present.
2. Run the required static and test gates.
3. Run the first-run help, dry-run, and smoke checks.
4. Run release/package integration checks in `artifacts/t19-release/`.
5. If any gate fails because of an implementation defect, add or update a
   focused regression test first, fix the defect narrowly, and rerun the failed
   gate plus any affected final gate.
6. Write the validation report with requirement-by-requirement status and
   command evidence.
7. Mark T19 complete in the backlog once all required gates are green.
8. Commit the local changes with a T19 validation message.

## Verification Commands

```bash
uv run --python 3.12 --extra dev ruff check src/awf tests
uv run --python 3.12 --extra dev mypy src/awf
uv run --python 3.12 --extra dev pytest tests/unit -q
uv run --python 3.12 --extra dev python scripts/generate_openapi.py --check
uv run --python 3.12 --extra dev pytest -n 20 --timeout=300 --cov=awf --cov-report=term-missing --cov-fail-under=99
uv run --python 3.12 --extra dev awf setup --help
uv run --python 3.12 --extra dev awf start --help
uv run --python 3.12 --extra dev awf init --help
uv run --python 3.12 --extra dev awf mcp serve --help
uv run --python 3.12 --extra dev awf setup --dry-run --source-checkout "$PWD" --format json
uv run --python 3.12 --extra dev python scripts/first_run_smoke.py --lane installer-fixture --lane source-uv-run --lane source-tool-install --checkout-root "$PWD"
mkdir -p artifacts/t19-release/dist artifacts/t19-release/release
uv run --python 3.12 --with build python -m build --outdir artifacts/t19-release/dist
sha256sum artifacts/t19-release/dist/* | tee artifacts/t19-release/release/python-distribution-sha256.txt
uv run --python 3.12 python scripts/generate_install_manifest.py \
  --dist-dir artifacts/t19-release/dist \
  --checksums-file artifacts/t19-release/release/python-distribution-sha256.txt \
  --output artifacts/t19-release/release/awf-install-manifest.json \
  --version 0.1.0 \
  --tag v0.1.0 \
  --repository-url https://github.com/dimileeh/aira-agent-workspace-fabric \
  --channel auto
uv run --python 3.12 python scripts/check_release_artifacts.py \
  --dist-dir artifacts/t19-release/dist \
  --checksums-file artifacts/t19-release/release/python-distribution-sha256.txt \
  --manifest artifacts/t19-release/release/awf-install-manifest.json \
  --version 0.1.0 \
  --tag v0.1.0 \
  --repository-url https://github.com/dimileeh/aira-agent-workspace-fabric
uv run --no-project --python 3.12 python scripts/release_smoke.py \
  --dist-dir artifacts/t19-release/dist \
  --manifest artifacts/t19-release/release/awf-install-manifest.json \
  --smoke-manifest-out artifacts/t19-release/release/awf-install-manifest.smoke.json \
  --method uv \
  --run
```

Pass criteria: every required command exits zero, full coverage is at least
99%, first-run smoke lanes pass or report only explicitly documented
environmental skips, and the backlog reflects T19 as complete.
