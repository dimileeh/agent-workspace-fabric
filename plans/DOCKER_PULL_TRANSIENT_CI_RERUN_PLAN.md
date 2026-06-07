# Plan — Classify Docker pull / service-container registry timeouts as transient CI

## Problem

A GitHub Actions job for PR #449 (`python-coverage-shards (2)`, run `27091023772`,
job `79954557998`) failed **before checkout/tests** while Docker pulled the
`postgres:16` service-container image. Raw log evidence:

```text
/usr/bin/docker pull postgres:16
Error response from daemon: Get "https://registry-1.docker.io/v2/": context deadline exceeded (Client.Timeout exceeded while awaiting headers)
net/http: request canceled while waiting for connection (Client.Timeout exceeded while awaiting headers)
Docker pull failed with exit code 1
```

This is transient GitHub/Docker registry infrastructure, not a code/test failure.
Today `runtime/pr_monitor._looks_like_transient_ci_failure(...)` returns `False`
for this log because none of `_CI_TRANSIENT_FAILURE_MARKERS` match. So
`_should_rerun_transient_ci(...)` is `False`, `decide(...)` returns `ReportCiFailure`,
and AWF invokes a CI repair **agent** instead of the cheap/correct
`RerunTransientCI` → GitHub failed-job rerun.

## Goal

Classify Docker pull / service-container registry **timeout** failures as transient
CI so `decide(...)` returns `RerunTransientCI`, without broadening matching so
ordinary Docker build/test failures look transient, and without weakening the
structured test/code-failure safeguards.

## Change

`src/awf/runtime/pr_monitor.py` — three narrowly-targeted network-timeout
markers, gated on Docker pull / daemon evidence (`_CI_DOCKER_REGISTRY_TIMEOUT_MARKERS`,
required alongside `_CI_DOCKER_PULL_EVIDENCE_MARKERS`):

- `"context deadline exceeded"` — Go/Docker daemon network-timeout signature.
- `"timeout exceeded while awaiting headers"` — net/http registry timeout.
- `"request canceled while waiting for connection"` — net/http connection-wait timeout.

These are transport-timeout signatures, but — unlike `_CI_TRANSIENT_FAILURE_MARKERS`
(`"tls handshake timeout"`, `"connection reset"`, `"recv failure"`) — they also appear
in genuine application/integration test failures (review PRRT_kwDOSJAM6s6Hppkd). So
`_looks_like_transient_ci_failure` only treats them as transient when the same log
shows Docker pull / daemon activity (`"docker pull"` / `"error response from daemon"`),
via the `_log_shows_docker_registry_timeout` helper. Deliberately **not** adding
generic `"docker pull failed"` / `"exit code 1"`, since a Docker build/run can fail for
genuine code reasons. The existing structured/code-failure short-circuit still wins
first; no change to `_should_rerun_transient_ci` or `decide`.

A bare `"docker pull"` echo precedes both successful and failed pulls, and
`gh run view --log-failed` emits the whole failed step — so a real integration/Go
test that logs `"context deadline exceeded"` can co-exist in the same excerpt as an
unrelated, successful setup pull (review PRRT_kwDOSJAM6s6HptkR). To keep that real
failure reaching the repair agent, `_log_shows_docker_registry_timeout` is
line-scoped: a registry-timeout marker only counts as Docker-caused when it sits on,
or within `_CI_DOCKER_TIMEOUT_EVIDENCE_WINDOW` (2) lines of, a Docker pull / daemon
line — not merely somewhere in the same step log.

## Tests (TDD, in `tests/unit/runtime/test_pr_monitor_parts/test_pr_monitor_part_003.py`)

> The CI-failure suite (`TestCiFailure`) was later moved from
> `test_pr_monitor_part_001.py` to `test_pr_monitor_part_003.py` when the part_001
> module hit the 1500-line guardrail; these tests live in part_003.

1. `test_docker_pull_registry_timeout_dispatches_rerun` — full PR #449 log → `RerunTransientCI`.
2. `test_docker_pull_request_canceled_dispatches_rerun` — minimal `docker pull` +
   `request canceled while waiting for connection` → `RerunTransientCI`.
3. `test_docker_pull_with_structured_test_evidence_reports_ci_failure` — Docker-pull
   timeout log + `test_node_ids`/`assertion_snippets` → `ReportCiFailure` (safeguard).
4. `test_successful_setup_pull_then_unrelated_test_timeout_reports_ci_failure` — a
   successful setup pull followed (many lines later) by a real test
   `context deadline exceeded` → `ReportCiFailure` (line-proximity safeguard).

## Validation (focused only)

```bash
uv run --python 3.12 --extra dev pytest \
  tests/unit/runtime/test_pr_monitor_parts/test_pr_monitor_part_003.py -q -k transient
uv run --python 3.12 --extra dev ruff check src/awf/runtime/pr_monitor.py tests/unit/runtime/test_pr_monitor_parts/test_pr_monitor_part_003.py
uv run --python 3.12 --extra dev ruff format --check src/awf/runtime/pr_monitor.py tests/unit/runtime/test_pr_monitor_parts/test_pr_monitor_part_003.py
uv run --python 3.12 --extra dev mypy
```

Full coverage/CI owned by AWF/GitHub after the agent finishes.

## Non-goals

- No change to `decide(...)` gate ordering or `_should_rerun_transient_ci` gates.
- No generic `docker`/`exit code` matching.
- No unrelated refactors.
