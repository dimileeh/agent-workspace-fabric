# Validation — Docker pull / service-container registry timeout transient CI

Implements `plans/DOCKER_PULL_TRANSIENT_CI_RERUN_PLAN.md`.

## Change summary

- `src/awf/runtime/pr_monitor.py`: classify Docker registry image-pull timeouts as
  transient CI via **proximity-gated matching** — network-timeout phrases count as
  transient only when they sit on, or within `_CI_DOCKER_TIMEOUT_EVIDENCE_WINDOW`
  (2) lines of, an explicit Docker pull-failure anchor. The shipped approach adds
  four constant groups and two helper functions, and **does not** touch
  `_CI_TRANSIENT_FAILURE_MARKERS`:
  - `_CI_DOCKER_REGISTRY_TIMEOUT_MARKERS` — `"context deadline exceeded"`,
    `"timeout exceeded while awaiting headers"`,
    `"request canceled while waiting for connection"`.
  - `_CI_DOCKER_SELF_EVIDENT_PULL_FAILURE_MARKER` (`"docker pull failed"`) — names
    the Docker CLI explicitly, so it anchors on its own.
  - `_CI_DOCKER_IMAGE_PULL_FAILURE_MARKER` (`"failed to pull image"`) +
    `_CI_DOCKER_PULL_CONTEXT_MARKERS` (`"docker pull"` echo / daemon error /
    registry hosts / `/v2/` / `"pull access denied"`) — this phrasing is shared by
    Docker, containerd, and the Kubernetes kubelet, so a bare
    `Failed to pull image "app": context deadline exceeded` e2e/k8s deploy bug must
    reach the repair agent. It anchors a registry timeout only when corroborating
    Docker pull context sits within `_CI_DOCKER_TIMEOUT_EVIDENCE_WINDOW` lines
    (review comment PRRT_kwDOSJAM6s6HqBMY).
  - `_CI_DOCKER_DAEMON_ERROR_MARKER` (`"error response from daemon"`) +
    `_CI_DOCKER_REGISTRY_PULL_CONTEXT_MARKERS` (registry hosts / `/v2/` API path /
    `"pull access denied"`) — a daemon-error line only anchors when it also carries
    registry/image-pull context. Markers stay specific: the generic phrase
    `"pulling from"` was dropped (review comment issue:4642392722) because it also
    matches non-registry daemon operations such as
    `failed while pulling from local volume`.
  - `_CI_DOCKER_TIMEOUT_EVIDENCE_WINDOW` — the line-proximity window.
  - `_is_docker_pull_failure_line(index, line, pull_context_indexes)` — whether one
    log line evidences a Docker image-pull failure (the `failed to pull image`
    branch requires Docker pull context within the proximity window).
  - `_log_shows_docker_registry_timeout(log_text)` — whether a registry-timeout
    phrase is line-co-located with a pull-failure line; called as the final clause
    of `_looks_like_transient_ci_failure`.
- The structured/code-failure short-circuit in `_looks_like_transient_ci_failure`
  still runs first, `_CI_TRANSIENT_FAILURE_MARKERS` is matched before the new
  logic, and `_should_rerun_transient_ci`/`decide` gate ordering is untouched.
- `tests/unit/runtime/test_pr_monitor_parts/test_pr_monitor_part_001.py`: added
  17 focused unit tests (TDD across the feature's commits). Grouped by intent:

  Positive — registry timeout dispatches `RerunTransientCI`:
  - `test_docker_pull_registry_timeout_dispatches_rerun` — full PR #449 log.
  - `test_docker_pull_failed_wording_anchors_request_canceled_rerun` —
    `"docker pull failed"` anchors a `request canceled` timeout.
  - `test_awaiting_headers_timeout_without_client_prefix_dispatches_rerun` —
    `"timeout exceeded while awaiting headers"` without a `Client.` prefix.
  - `test_docker_failed_to_pull_image_timeout_dispatches_rerun` —
    `"failed to pull image"` anchor corroborated by a nearby `docker pull` echo.
  - `test_daemon_error_with_registry_url_timeout_dispatches_rerun` — daemon error
    carrying registry-URL context.

  Negative — non-Docker / unanchored timeouts report `ReportCiFailure`:
  - `test_docker_pull_echo_then_bare_request_canceled_reports_ci_failure` — a bare
    `docker pull` command echo does not anchor.
  - `test_registry_timeout_phrase_without_docker_pull_reports_ci_failure`.
  - `test_context_deadline_exceeded_without_docker_pull_reports_ci_failure`.
  - `test_successful_setup_pull_then_unrelated_test_timeout_reports_ci_failure` —
    far-apart successful pull + real test timeout (line-proximity safeguard).
  - `test_compact_cached_pull_then_test_timeout_at_window_reports_ci_failure` —
    cached pull + test timeout at window distance.
  - `test_app_failed_to_pull_records_timeout_reports_ci_failure` — generic
    `failed to pull records` app error, no Docker/image wording.
  - `test_daemon_error_timeout_without_pull_context_reports_ci_failure` — daemon
    error with no registry context.
  - `test_daemon_error_generic_pulling_from_phrase_reports_ci_failure` — a daemon
    error whose text merely contains the generic phrase `pulling from`
    (e.g. `failed while pulling from local volume`) does not anchor a nearby
    timeout (review comment issue:4642392722).
  - `test_k8s_failed_to_pull_image_without_docker_context_reports_ci_failure` — a
    bare kubelet/containerd `Failed to pull image "app": context deadline exceeded`
    e2e deploy event with no `docker pull`/daemon/registry context reaches the
    repair agent rather than being silently rerun (review comment
    PRRT_kwDOSJAM6s6HqBMY).
  - `test_unrecognized_failure_log_reports_ci_failure` — unrecognized log.

  Safeguard / helper:
  - `test_docker_pull_with_structured_test_evidence_reports_ci_failure` — structured
    test evidence still forces `ReportCiFailure` ahead of the new logic.
  - `test_log_shows_docker_registry_timeout_lowercases_raw_text` — the helper
    lowercases raw text so it is self-contained.

## Commands run (focused)

```
uv run --python 3.12 --extra dev pytest \
  tests/unit/runtime/test_pr_monitor_parts/test_pr_monitor_part_001.py -q \
  -k "docker or registry_timeout or awaiting_headers or request_canceled or \
      context_deadline or daemon_error or failed_to_pull or unrecognized_failure or \
      log_shows_docker or app_failed_to_pull or setup_pull or cached_pull or \
      pulling_from"
=> 16 passed, 96 deselected

uv run --python 3.12 --extra dev ruff check \
  src/awf/runtime/pr_monitor.py \
  tests/unit/runtime/test_pr_monitor_parts/test_pr_monitor_part_001.py
=> All checks passed!

uv run --python 3.12 --extra dev ruff format --check \
  src/awf/runtime/pr_monitor.py \
  tests/unit/runtime/test_pr_monitor_parts/test_pr_monitor_part_001.py
=> 2 files already formatted

uv run --python 3.12 --extra dev mypy
=> Success: no issues found in 354 source files
```

## Coverage reasoning

Both new helper functions and every new constant group are exercised by the 16
tests above: the positive tests drive the `RerunTransientCI` path through each
anchor (pull-failed wording, image-pull wording, daemon-error + registry context)
and each timeout marker; the negative tests cover the unanchored, far-proximity,
generic-app-error, and no-context branches that must fall through to
`ReportCiFailure`; the structured-evidence and lowercasing tests cover the
short-circuit and helper-normalization branches. No new uncovered lines or
branches are introduced. Full coverage gate (`pytest --cov`) and CI-equivalent
suites are owned by AWF/GitHub after the agent finishes and were not run locally
per the AWF workspace contract.

## Safeguards preserved

- Structured test/code-failure evidence (`test_node_ids`/`assertion_snippets`)
  still forces `ReportCiFailure` even when a registry-timeout marker is present
  (locked in by `test_docker_pull_with_structured_test_evidence_reports_ci_failure`).
- Registry-timeout markers are only transient when line-co-located with a Docker
  pull-*failure* line, so a real application/integration test timeout that merely
  co-exists with an unrelated successful setup pull in the same `--log-failed`
  excerpt still reaches the repair agent.
- No generic `docker pull` echo / `exit code 1` marker added, so ordinary Docker
  build/test failures without a co-located registry-timeout phrase remain
  non-transient.
