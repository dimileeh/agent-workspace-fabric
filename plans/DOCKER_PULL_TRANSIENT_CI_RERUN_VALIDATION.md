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
  - `_CI_DOCKER_IMAGE_PULL_FAILURE_MARKER` (`"failed to pull image"`) corroborated
    **only** by a `_CI_DOCKER_PULL_COMMAND_MARKER` (`"docker pull"`) echo that
    targets the same image ref (matched via
    `_CI_DOCKER_IMAGE_PULL_FAILURE_REF_PATTERN`). This phrasing is shared by
    Docker, containerd, and the Kubernetes kubelet, so a bare
    `Failed to pull image "app": context deadline exceeded` e2e/k8s deploy bug must
    reach the repair agent. It anchors a registry timeout only when corroborating
    Docker pull context sits within `_CI_DOCKER_TIMEOUT_EVIDENCE_WINDOW` lines
    (review comment PRRT_kwDOSJAM6s6HqBMY). The same-ref requirement on the `docker
    pull` echo (review comment PRRT_kwDOSJAM6s6HqJ-I) prevents a *successful*
    service-container setup `docker pull postgres:16` from corroborating a kubelet
    `failed to pull image "app"` bug for an unrelated application image purely by
    line distance; the ref is compared as a whitespace-delimited token so a
    `docker pull myapp:1` echo cannot satisfy a `"app"` failure by substring.
    Registry-*protocol* proximity corroboration (formerly
    `_CI_DOCKER_REGISTRY_PROTOCOL_CONTEXT_MARKERS` = `/v2/` / `"pull access
    denied"`) was **removed** (review comment PRRT_kwDOSJAM6s6HqN1K): a
    kubelet/containerd application-image event embeds the `/v2/` transport URL in
    its own (often multi-line) error — e.g. `Failed to pull image
    "ghcr.io/org/app": ... Head "https://ghcr.io/v2/...": context deadline
    exceeded` — so a nearby `/v2/` let the failing line self-corroborate and rerun
    a real deploy bug, exactly as a bare registry host on the ref would. Genuine
    Docker/service-container pull failures are still covered by the same-ref
    `docker pull` echo, the `docker pull failed` marker, and the daemon-error
    branch. A
    bare `"error response from daemon"` line is deliberately excluded from pull
    context (review comment PRRT_kwDOSJAM6s6HqEY1): the daemon emits it for any
    operation, so a generic daemon timeout adjacent to a kubelet `failed to pull
    image` event must not corroborate it; a daemon line that genuinely carries
    registry context still counts via the registry markers, keeping it consistent
    with the same-line requirement in `_is_docker_pull_failure_line`.
  - `_CI_DOCKER_DAEMON_ERROR_MARKER` (`"error response from daemon"`) +
    `_CI_DOCKER_REGISTRY_PULL_CONTEXT_MARKERS` (registry *request* forms: the
    `/v2/` distribution-API path and the `auth.docker.io` token host) — a
    daemon-error line only anchors when it also carries an outbound registry
    *request* form. Markers stay specific: the generic phrase `"pulling from"` was
    dropped (review comment issue:4642392722) because it also matches non-registry
    daemon operations such as `failed while pulling from local volume`. `"pull
    access denied"` was likewise dropped (review comment issue:4642392722): it is a
    synchronous registry 403, so it can never itself be the timeout, and accepting
    it let a real (permanent) auth failure anchor an *adjacent unrelated* timeout
    and silently rerun it instead of surfacing the auth bug to the repair agent.
    The bare image-reference registry hosts (`ghcr.io`, `gcr.io`, `quay.io`,
    `public.ecr.aws`, `*.pkg.dev`, and the Docker endpoint hosts) were then dropped
    too (review comment PRRT_kwDOSJAM6s6HqXaV): they sit on the image *ref* of
    permanent, non-timeout daemon errors (`pull access denied for ghcr.io/org/app`,
    `No such image: ghcr.io/org/app`), so a bare host let such a permanent
    auth/image error anchor an adjacent unrelated `context deadline exceeded` and
    silently rerun it. A genuine registry pull timeout against any of those hosts
    already carries the `/v2/` request path (`Get "https://ghcr.io/v2/...":
    context deadline exceeded`), so the request-form markers preserve real
    transient detection while the bare host no longer poses as a pull request.
  - `_CI_DOCKER_TIMEOUT_EVIDENCE_WINDOW` — the line-proximity window.
  - `_is_docker_pull_failure_line(index, line, lines,
    docker_pull_command_indexes)` — whether one log line evidences a Docker
    image-pull failure; delegates the `failed to pull image` branch to
    `_image_pull_failure_is_corroborated(...)`, which requires a same-ref `docker
    pull` echo within the window (registry-protocol proximity corroboration was
    removed — see review comment PRRT_kwDOSJAM6s6HqN1K above).
  - `_log_shows_docker_registry_timeout(log_text)` — whether a registry-timeout
    phrase is line-co-located with a pull-failure line; called as the final clause
    of `_looks_like_transient_ci_failure`.
- The structured/code-failure short-circuit in `_looks_like_transient_ci_failure`
  still runs first, `_CI_TRANSIENT_FAILURE_MARKERS` is matched before the new
  logic, and `_should_rerun_transient_ci`/`decide` gate ordering is untouched.
- `tests/unit/runtime/test_pr_monitor_parts/test_pr_monitor_part_003.py`: added
  focused unit tests (TDD across the feature's commits; the `TestCiFailure` suite
  was moved here from `test_pr_monitor_part_001.py` when part_001 hit the 1500-line
  guardrail). Grouped by intent:

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
  - `test_unrelated_setup_pull_echo_does_not_corroborate_image_failure` — a
    *successful* `docker pull postgres:16` setup echo within the proximity window of
    a kubelet `Failed to pull image "app"` event for an unrelated image does not
    corroborate it (different ref), so the real app-image bug reaches the repair
    agent (review comment PRRT_kwDOSJAM6s6HqJ-I).
  - `test_k8s_failed_to_pull_image_with_embedded_v2_url_reports_ci_failure` — a
    kubelet/containerd `Failed to pull image "ghcr.io/org/app"` event whose own
    error embeds the registry transport URL (`Head "https://ghcr.io/v2/...":
    context deadline exceeded`) does not self-corroborate: the `/v2/` is part of
    the kubelet event, not separate Docker CLI evidence, so the real deploy bug
    reaches the repair agent (review comment PRRT_kwDOSJAM6s6HqN1K).
  - `test_unrecognized_failure_log_reports_ci_failure` — unrecognized log.

  Safeguard / helper:
  - `test_docker_pull_with_structured_test_evidence_reports_ci_failure` — structured
    test evidence still forces `ReportCiFailure` ahead of the new logic.
  - `test_log_shows_docker_registry_timeout_lowercases_raw_text` — the helper
    lowercases raw text so it is self-contained.

## Commands run (focused)

```bash
uv run --python 3.12 --extra dev pytest \
  tests/unit/runtime/test_pr_monitor_parts/test_pr_monitor_part_003.py -q \
  -k "docker or registry_timeout or awaiting_headers or request_canceled or \
      context_deadline or daemon_error or failed_to_pull or unrecognized_failure or \
      log_shows_docker or app_failed_to_pull or setup_pull or cached_pull or \
      pulling_from or image"
=> 28 passed, 28 deselected

uv run --python 3.12 --extra dev ruff check \
  src/awf/runtime/pr_monitor.py \
  tests/unit/runtime/test_pr_monitor_parts/test_pr_monitor_part_003.py
=> All checks passed!

uv run --python 3.12 --extra dev ruff format --check \
  src/awf/runtime/pr_monitor.py \
  tests/unit/runtime/test_pr_monitor_parts/test_pr_monitor_part_003.py
=> 2 files already formatted

uv run --python 3.12 --extra dev mypy
=> Success: no issues found in 354 source files
```

## Coverage reasoning

Both helper functions (now including `_image_pull_failure_is_corroborated`) and
every constant group are exercised by the tests above: the positive tests drive
the `RerunTransientCI` path through each anchor (pull-failed wording, same-ref
image-pull wording, daemon-error + registry context) and each timeout marker; the
negative tests cover the unanchored, far-proximity, different-ref echo,
generic-app-error, and no-context branches that must fall through to
`ReportCiFailure`; the structured-evidence and lowercasing tests cover the
short-circuit and helper-normalization branches. The new same-ref branch is
covered by `test_unrelated_setup_pull_echo_does_not_corroborate_image_failure`
(different ref → no corroboration) and
`test_docker_failed_to_pull_image_timeout_dispatches_rerun` (same ref →
corroboration). No new uncovered lines or branches are introduced. Full coverage gate (`pytest --cov`) and CI-equivalent
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

## Follow-up — same-ref echo must evidence a *failed* pull (PRRT_kwDOSJAM6s6Hqh9w)

A same-ref `docker pull <ref>` *command* echo is printed for **successful**
pre-pulls too, so it alone is not evidence that the same-ref Docker pull failed.
`_image_pull_failure_is_corroborated` now also requires that the echoed pull did
**not** print a ref-bearing success status
(`_CI_DOCKER_PULL_SUCCESS_STATUS_MARKERS`: `"status: downloaded newer image
for <ref>"` / `"status: image is up to date for <ref>"`) between the echo and the
`failed to pull image "<ref>"` line, via the new `_docker_pull_command_succeeded`
helper. Otherwise a successful same-ref pre-pull adjacent to a kubelet `Failed to
pull image "<same ref>"` event (a real deploy bug) would be silently rerun as
transient infra.

The success-status check matches the ref as a whitespace-delimited **token**
(`image_ref in probe.split()`), mirroring the `docker pull` echo match, so a
success status for a *different* image whose name merely has the failed ref as a
prefix (`Status: Downloaded newer image for app-db` vs a failed `app` pull) does
not spuriously suppress a genuine same-ref pull failure.

- Covered by `test_successful_same_ref_pre_pull_does_not_corroborate_image_failure`
  (success status present → `ReportCiFailure`).
- Covered by `test_prefix_ref_success_status_does_not_suppress_same_ref_pull_failure`
  (different prefix-overlapping ref's success status → genuine same-ref failure
  still `RerunTransientCI`; locks in the token-bounded match).
- The positive `test_docker_failed_to_pull_image_timeout_dispatches_rerun` (no
  success status between echo and failure → still `RerunTransientCI`) locks in that
  genuine same-ref pull failures remain transient, exercising the
  `_docker_pull_command_succeeded` False branch.

## Follow-up — uncorroborated-event guard covers wrapped multi-line errors (issue:4642392722)

The original uncorroborated-event guard in `_log_shows_docker_registry_timeout`
only suppressed a registry-timeout marker that sat **on** an uncorroborated
`failed to pull image` line. A kubelet/containerd event can wrap its error onto a
*following* line (`Failed to pull image "app"` then a bare `context deadline
exceeded` on the next line); that timeout line carries no `failed to pull image`
text of its own, so the on-line-only guard let it be attributed to a nearby
unrelated transient `docker pull failed` anchor within the proximity window and
silently rerun a real deploy bug. The guard now excludes any timeout marker within
`_CI_DOCKER_TIMEOUT_EVIDENCE_WINDOW` lines of an uncorroborated `failed to pull
image` line (which subsumes the same-line case), so the wrapped variant reaches the
repair agent.

- Covered by
  `test_wrapped_uncorroborated_image_event_timeout_near_transient_pull_reports_ci_failure`
  (wrapped timeout → `ReportCiFailure`), the companion to the same-line
  `test_uncorroborated_image_event_timeout_near_transient_pull_reports_ci_failure`.
- The positive boundary test
  `test_registry_timeout_at_exact_window_boundary_dispatches_rerun` (no
  uncorroborated `failed to pull image` line present → still `RerunTransientCI`)
  locks in that the new exclusion does not over-fire on genuine transient pulls.

## Follow-up — uncorroborated guard must exempt self-evident daemon timeouts (PRRT_kwDOSJAM6s6Hqr2g)

The wrapped-error fix above excludes *any* timeout marker within
`_CI_DOCKER_TIMEOUT_EVIDENCE_WINDOW` lines of an uncorroborated `failed to pull
image` line. That over-reached: when the timeout line is *itself* a Docker
pull-*failure* evidence line — a self-evident daemon registry timeout such as
`Error response from daemon: Get "https://registry-1.docker.io/v2/": context
deadline exceeded` — an uncorroborated `Failed to pull image "app"` summary
printed immediately before it (no double-quoted ref to corroborate, or a wrapped
summary) dragged the genuine timeout out of the transient set, so a real registry
flake reached the repair agent instead of being rerun. The guard now exempts
timeout lines whose own index is in `evidence_line_set` (they are clear registry
evidence on their own), keeping the uncorroborated exclusion only for timeout
phrases that are *not* themselves pull-failure evidence (the wrapped-kubelet case).

- Covered by
  `test_uncorroborated_summary_does_not_suppress_self_evident_daemon_timeout`
  (uncorroborated summary immediately before a self-evident daemon registry
  timeout → `RerunTransientCI`).
- The existing wrapped/same-line guards
  (`test_wrapped_uncorroborated_image_event_timeout_near_transient_pull_reports_ci_failure`,
  `test_uncorroborated_image_event_timeout_near_transient_pull_reports_ci_failure`)
  still pass — their timeout lines are bare kubelet phrases, not daemon evidence
  lines, so they remain excluded and reach the repair agent.

## Follow-up — `docker pull failed` summary must not be read as a command echo (issue:4642392722)

`docker_pull_command_indexes` in `_log_shows_docker_registry_timeout` collected
every line containing the `docker pull` substring as a `docker pull <ref>` *command
echo*. A `docker pull failed with exit code 1` *failure summary* also contains that
substring, so it was added too. In `_image_pull_failure_is_corroborated` the
corroboration check is `image_ref in lines[context_index].split()`; that summary line
splits to `["docker", "pull", "failed", "with", "exit", "code", "1"]`, so an adjacent
`Failed to pull image "docker"` kubelet event — `docker` is a real Docker Hub image —
matched on the `docker` token and was wrongly treated as corroborated Docker-CLI
evidence. That dropped the real application-image event out of
`uncorroborated_image_pull_indexes`, so its own `context deadline exceeded` was no
longer excluded and a real deploy bug would be silently rerun. The command-index set
now excludes self-evident pull-failure lines
(`_CI_DOCKER_SELF_EVIDENT_PULL_FAILURE_MARKER not in line`); those already anchor as
their own evidence, so genuine same-ref corroboration via real `docker pull <ref>`
echoes is unaffected.

- Covered by
  `test_docker_pull_failed_summary_does_not_corroborate_same_token_image_event`
  (kubelet `Failed to pull image "docker"` next to a `Docker pull failed with exit
  code 1` summary → `ReportCiFailure`; fails as `RerunTransientCI` without the
  command-index exclusion).
- The existing positive
  `test_docker_pull_failed_wording_anchors_request_canceled_rerun` still passes —
  its real `docker pull postgres:16` echo line corroborates the genuine transient
  pull, so it remains `RerunTransientCI`.

### Deferred — duplicate `_thread`/`_review`/`_status` test helpers

The review also noted that `_thread`, `_review`, and `_status` are defined
identically across `test_pr_monitor_part_001.py`, `_part_002.py`, and `_part_003.py`.
This is a pre-existing three-way duplication from the module split, not a correctness
issue; centralizing the helpers (e.g. a shared `conftest.py`) is a maintainability
follow-up left out of this bug-fix change to keep the diff minimal and scoped.
