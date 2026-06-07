"""Docker pull / OCI registry transient-failure detection for the PR monitor.

Extracted from ``pr_monitor`` to keep that module under the first-party
line-count guardrail.  All functions here operate on plain strings and
integers — no domain types — so they are independently testable and
importable without pulling in the rest of the monitor's data model.

The public entry point is ``_log_shows_docker_registry_timeout``.
"""

from __future__ import annotations

import re

# Docker pull / service-container registry timeouts. Unlike the unconditional
# markers above, these Go context-timeout and net/http phrases also surface in
# genuine application or integration test failures (an outbound HTTP/gRPC call
# that times out is a real bug for the repair agent, not flaky infra). They only
# count as transient CI when the same log also shows Docker pull / daemon
# activity — i.e. a registry image pull that timed out before the job's real
# work ran — so a real test failure that merely logs one of these phrases is
# still reported instead of silently rerun.
_CI_DOCKER_REGISTRY_TIMEOUT_MARKERS = (
    "context deadline exceeded",
    "timeout exceeded while awaiting headers",
    "request canceled while waiting for connection",
)

# A bare ``docker pull`` *command* echo precedes both successful image pulls and
# pull failures, so it cannot anchor causation: a successful setup pull followed
# by an unrelated test timeout looks identical to a real registry timeout. Only
# Docker pull-*failure* wording — an explicit pull-failed message — actually
# evidences that a pull (not the job's real work) is what timed out.
#
# ``docker pull failed`` names the Docker CLI explicitly, so it evidences a Docker
# image-pull failure on its own. ``failed to pull image`` is handled separately
# below: a bare ``failed to pull`` phrase appears in real application errors (e.g.
# ``failed to pull records: context deadline exceeded``), and the narrower ``failed
# to pull image`` phrasing is shared by Docker, containerd, *and* the Kubernetes
# kubelet (``Failed to pull image "app": context deadline exceeded``) — so on its
# own it cannot tell a CI registry/service-container setup pull (transient infra)
# apart from an application-image pull inside a k8s/containerd e2e deployment (a
# real image/deploy bug). It therefore anchors only with corroborating Docker pull
# context — see ``_CI_DOCKER_IMAGE_PULL_FAILURE_MARKER``.
_CI_DOCKER_SELF_EVIDENT_PULL_FAILURE_MARKER = "docker pull failed"

# ``Error response from daemon: ...`` is emitted for *any* Docker daemon operation
# (``docker run``/``build``/``exec``, container start, healthcheck), not just image
# pulls. A bare ``Error response from daemon: context deadline exceeded`` from an
# ordinary build/test step is a real Docker daemon timeout the repair agent must
# see, not flaky registry infra. So a daemon-error line only anchors a registry
# timeout when the *same line* also carries both evidence of an outbound registry
# *request* and a registry timeout marker — i.e. the daemon was reaching the
# registry when it timed out, not failing it permanently.
#
# That evidence must be a registry *request* form, **not** merely a registry host:
# a bare image-reference host (``ghcr.io``, ``gcr.io``, ``quay.io``,
# ``public.ecr.aws``, ``*.pkg.dev``) appears on the image *ref* of *permanent*,
# non-timeout daemon errors too — ``Error response from daemon: pull access denied
# for ghcr.io/org/app`` (a synchronous 403) or ``No such image: ghcr.io/org/app``.
# Accepting the bare host would let such a permanent auth/image error anchor an
# *adjacent unrelated* ``context deadline exceeded`` and silently rerun a real bug
# as transient infra. So only request-form markers qualify:
#   * ``/v2/`` — the registry distribution-API path. Every real image manifest/blob
#     pull request to *any* registry (Docker Hub, GHCR, GCR, Quay, ECR, Artifact
#     Registry) is ``https://<host>/v2/<repo>/...``, so a registry pull timeout
#     against any of those hosts already carries ``/v2/`` (e.g. ``Error response
#     from daemon: Get "https://ghcr.io/v2/org/app/manifests/v1": context deadline
#     exceeded``). A bare image-ref host on a permanent error carries no ``/v2/``.
#   * ``auth.docker.io`` — the Docker Hub registry-auth token service. Pulling from
#     Docker Hub first fetches a bearer token from ``auth.docker.io/token``, so a
#     daemon timeout reported against that host (``Error response from daemon: Get
#     "https://auth.docker.io/token?...": context deadline exceeded``) is a registry
#     pull failure even though it names no ``/v2/`` path. That host is contacted
#     *only* for registry operations and can never be an image ref, so it is a
#     request-form marker on its own.
#   * ``/token?`` — the Bearer-auth token endpoint request of *non*-Docker-Hub
#     registries. GHCR/ACR/Harbor and friends select their own token host (``Get
#     "https://ghcr.io/token?service=...&scope=...": context deadline exceeded``),
#     which is neither ``/v2/`` nor ``auth.docker.io``, so without this marker a real
#     GHCR token-fetch timeout would never anchor and the registry flake would be
#     reported instead of rerun. The ``?`` query makes it unambiguously a token
#     *request* URL, never an image ref (a bare ``.../token`` repo path on a permanent
#     denial carries no ``?`` query and, lacking the timeout marker, never anchors
#     either).
# A generic phrase such as ``pulling from`` is likewise excluded: it would match
# unrelated daemon operations (e.g. ``failed while pulling from local volume``).
# The registry timeout marker is required on that line because the request form
# alone does not separate a timed-out pull from a *permanent* registry denial that
# also quotes the ``/v2/`` request — ``Error response from daemon: Head
# "https://ghcr.io/v2/org/app/manifests/latest": denied`` (or ``: unauthorized``) is a
# synchronous 401/403, not a timeout. Without the timeout-marker requirement such a
# permanent auth error would anchor an *adjacent unrelated* ``context deadline
# exceeded`` and silently rerun a real auth bug as transient infra.
_CI_DOCKER_DAEMON_ERROR_MARKER = "error response from daemon"
_CI_DOCKER_REGISTRY_PULL_CONTEXT_MARKERS = (
    "/v2/",
    "auth.docker.io",
    "/token?",
)

# ``failed to pull image "<ref>"`` is emitted by Docker, containerd, and the
# Kubernetes kubelet alike. A bare kubelet/containerd ``Failed to pull image
# "app"`` event for an *application* image in an e2e deployment is a real
# image/deploy bug the repair agent must see, so this marker only anchors a
# registry timeout when a ``docker pull`` command echo *for the same image ref*
# sits within ``_CI_DOCKER_TIMEOUT_EVIDENCE_WINDOW`` lines, i.e. the failing pull
# went through the Docker CLI rather than a bare kubelet event. The same-ref
# requirement on the ``docker pull`` echo matters because that echo also precedes
# *successful* setup pulls: a successful ``docker pull postgres:16`` next to a
# kubelet ``failed to pull image "app"`` event for an unrelated application image
# must not corroborate it by mere line distance.
#
# Registry-*protocol* wording (a ``/v2/`` API request) is deliberately *not*
# accepted as proximity corroboration here, even though the daemon-error branch
# above uses ``/v2/`` as same-line context. A
# kubelet/containerd application-image event embeds that same wording in its own
# (often multi-line) transport error — e.g. ``Failed to pull image
# "ghcr.io/org/app": ... Head "https://ghcr.io/v2/...": context deadline
# exceeded`` — so accepting a nearby ``/v2/`` lets the ``failed to pull image``
# line self-corroborate and silently rerun a real application-image bug, exactly
# as a bare registry *host* (``ghcr.io``) on the ref would. The Docker-CLI
# ``docker pull`` echo (same ref) and the ``docker pull failed`` / daemon-error
# branches already cover genuine Docker/service-container pull failures.
#
# A bare ``error response from daemon`` line is likewise *not* pull context: the
# daemon emits it for any operation (``docker run``/``build``/start), so a
# generic daemon timeout adjacent to a kubelet ``failed to pull image`` event
# must not corroborate it. A daemon line only counts when it *also* carries
# registry context — which the registry markers above already capture — keeping
# this consistent with the same-line requirement in ``_is_docker_pull_failure_line``.
_CI_DOCKER_IMAGE_PULL_FAILURE_MARKER = "failed to pull image"
# The quoted image ref on a ``failed to pull image "<ref>"`` line, used to confirm
# that a corroborating ``docker pull`` echo targets the *same* image (see below).
_CI_DOCKER_IMAGE_PULL_FAILURE_REF_PATTERN = re.compile(r'failed to pull image\s+"([^"]+)"')
# A bare ``docker pull`` *command* echo precedes successful setup pulls too, so it
# corroborates a ``failed to pull image`` line only when it targets the *same*
# image ref — proximity alone would let a successful ``docker pull postgres:16``
# setup echo license rerunning a kubelet ``failed to pull image "app"`` bug for an
# unrelated application image.
_CI_DOCKER_PULL_COMMAND_MARKER = "docker pull"

# A *successful* ``docker pull`` prints a ref-bearing terminal status — ``Status:
# Downloaded newer image for <ref>`` or ``Status: Image is up to date for <ref>``.
# Such a line between a ``docker pull <ref>`` echo and a same-ref ``failed to pull
# image "<ref>"`` line proves the echoed pull *succeeded*, so the failure is a
# separate kubelet/containerd deploy event (a real image bug), not the echoed
# command's own output. The same-ref command echo alone is not evidence the pull
# failed — a successful pre-pull emits one too — so it must not corroborate.
_CI_DOCKER_PULL_SUCCESS_STATUS_MARKERS = (
    "status: downloaded newer image for ",
    "status: image is up to date for ",
)

# Permanent (non-retryable) Docker pull / OCI registry error strings.  When any
# of these phrases appear on a Docker pull-failure evidence line — or on a nearby
# ``error response from daemon`` line within the timeout-evidence window — the pull
# cannot succeed on a retry (auth denial, missing image/tag, unknown manifest).
# These are checked against a narrow set of targets so generic terms like
# ``"not found"`` and ``"unauthorized"`` do not fire on unrelated application log
# lines that happen to sit near a ``docker pull failed`` summary.
_CI_DOCKER_PERMANENT_PULL_ERROR_MARKERS = (
    "access denied",
    "denied:",
    "no such image",
    "manifest unknown",
    "not found",
    "unauthorized",
    "repository does not exist",
)

# ``gh run view --log-failed`` emits the whole failed step, so a real
# integration/Go test that logs ``context deadline exceeded`` can sit in the same
# excerpt as an unrelated Docker pull failure. A registry-timeout marker
# therefore only counts as Docker-caused when it is on (or within this many lines
# of) a Docker pull-failure line — not merely somewhere in the same step log.
_CI_DOCKER_TIMEOUT_EVIDENCE_WINDOW = 2


def _is_docker_pull_failure_line(
    index: int,
    line: str,
    lines: list[str],
    docker_pull_command_indexes: tuple[int, ...],
) -> bool:
    """Whether one (lowercased) log line evidences a Docker *image-pull* failure.

    ``docker pull failed`` names the Docker CLI, so it qualifies on its own. The
    ``failed to pull image`` wording is shared with containerd / the Kubernetes
    kubelet, so it qualifies only when a ``docker pull`` command echo *targeting
    the same image ref* sits within ``_CI_DOCKER_TIMEOUT_EVIDENCE_WINDOW`` lines —
    the echo precedes successful setup pulls too, so a successful ``docker pull
    postgres:16`` next to a kubelet ``failed to pull image "app"`` event must not
    corroborate an unrelated application-image bug by mere line distance. A bare
    registry *host*, and likewise a registry-*protocol* ``/v2/`` request, is not
    such context: a registry-qualified ``failed to pull image "ghcr.io/org/app"``
    event carries the host on the ref and embeds the ``/v2/`` transport URL in its
    own error, so either would self-corroborate.
    Otherwise a bare kubelet ``failed to pull image "app"`` event from an e2e
    deployment (a real image bug) would be silently rerun. A bare ``error response
    from daemon`` line is not such context: the daemon emits it for any operation,
    so a generic daemon timeout next to a kubelet ``failed to pull image`` event
    must not corroborate it. The daemon wrapper anchors only as its own evidence
    line, and only when that same line also carries *both* a registry *request* form
    (``_CI_DOCKER_REGISTRY_PULL_CONTEXT_MARKERS`` — a ``/v2/`` distribution-API path,
    the ``auth.docker.io`` token host, or a non-Docker-Hub ``/token?`` Bearer-auth
    request) *and* a registry timeout marker
    (``_CI_DOCKER_REGISTRY_TIMEOUT_MARKERS``). A bare image-reference host such as
    ``ghcr.io`` is **not** such evidence: it also sits on the image ref of permanent
    daemon errors (``pull access denied for ghcr.io/org/app``, ``No such image:
    ghcr.io/org/app``), so accepting it would let a real auth/image bug anchor an
    adjacent unrelated timeout. The request form alone is not enough either: a
    *permanent* denial can quote the ``/v2/`` request (``Error response from daemon:
    Head "https://ghcr.io/v2/org/app/manifests/latest": denied`` / ``: unauthorized``)
    without timing out, so requiring the timeout marker on the same line keeps such an
    auth bug from anchoring an adjacent unrelated timeout. A bare daemon timeout from
    a ``docker run`` test step is likewise a real failure, not flaky registry infra.
    """

    if _CI_DOCKER_SELF_EVIDENT_PULL_FAILURE_MARKER in line:
        return True
    if _CI_DOCKER_IMAGE_PULL_FAILURE_MARKER in line and _image_pull_failure_is_corroborated(
        index, line, lines, docker_pull_command_indexes
    ):
        return True
    return (
        _CI_DOCKER_DAEMON_ERROR_MARKER in line
        and any(marker in line for marker in _CI_DOCKER_REGISTRY_PULL_CONTEXT_MARKERS)
        and any(marker in line for marker in _CI_DOCKER_REGISTRY_TIMEOUT_MARKERS)
    )


def _image_pull_failure_is_corroborated(
    index: int,
    line: str,
    lines: list[str],
    docker_pull_command_indexes: tuple[int, ...],
) -> bool:
    """Whether a ``failed to pull image`` line has nearby Docker pull context.

    The only trustworthy corroboration is a ``docker pull`` command echo that
    targets the *same* image ref as the failing line — compared as a
    whitespace-delimited token so a ``docker pull myapp:1`` echo cannot satisfy a
    ``failed to pull image "app"`` failure by substring. Registry-*protocol*
    wording (a ``/v2/`` request) is deliberately *not* accepted by proximity: a
    kubelet/containerd application-image event embeds
    that same wording in its own (often multi-line) transport error — e.g.
    ``Failed to pull image "ghcr.io/org/app": ... Head "https://ghcr.io/v2/...":
    context deadline exceeded`` — so it would let a real deploy bug
    self-corroborate and be silently rerun.

    The same-ref echo must also evidence a *failed* pull, not merely that a pull
    command appeared: a successful same-ref pre-pull echoes ``docker pull
    ghcr.io/org/app`` too, and a kubelet ``Failed to pull image
    "ghcr.io/org/app"`` event for that same ref nearby would then be silently
    rerun as transient infra even though it is a real deploy bug. A successful
    pull prints a ref-bearing ``Status: ...`` success line
    (``_CI_DOCKER_PULL_SUCCESS_STATUS_MARKERS``) between the echo and the failure,
    so an echo proven successful that way does not corroborate.
    """

    match = _CI_DOCKER_IMAGE_PULL_FAILURE_REF_PATTERN.search(line)
    if match is None:
        return False
    image_ref = match.group(1)
    return any(
        abs(index - context_index) <= _CI_DOCKER_TIMEOUT_EVIDENCE_WINDOW
        and image_ref in lines[context_index].split()
        and not _docker_pull_command_succeeded(image_ref, context_index, index, lines)
        for context_index in docker_pull_command_indexes
    )


def _docker_pull_command_succeeded(
    image_ref: str,
    command_index: int,
    failure_index: int,
    lines: list[str],
) -> bool:
    """Whether the ``docker pull <ref>`` echo printed a same-ref success status.

    A successful ``docker pull`` emits a ref-bearing terminal status — ``Status:
    Downloaded newer image for <ref>`` / ``Status: Image is up to date for
    <ref>``. When such a line sits between the echo and the ``failed to pull
    image "<ref>"`` line, the echoed pull *succeeded*, so the failure is a
    separate kubelet/containerd deploy event (a real bug) rather than the echoed
    command's output, and the echo must not corroborate it.

    The ref is matched as a whitespace-delimited token (as the ``docker pull``
    echo match is), not by substring, so a success status for a *different* image
    whose name merely has ``<ref>`` as a prefix (``Status: Downloaded newer image
    for app-db`` vs a failed ``app`` pull) does not spuriously suppress a genuine
    same-ref pull failure.
    """

    lower, upper = sorted((command_index, failure_index))
    for probe_index in range(lower + 1, upper):
        probe = lines[probe_index]
        if image_ref in probe.split() and any(
            marker in probe for marker in _CI_DOCKER_PULL_SUCCESS_STATUS_MARKERS
        ):
            return True
    return False


def _forward_detail_ref_matches_pull(
    detail_line: str, back_start: int, summary_index: int, lines: list[str]
) -> bool:
    """Whether a ``failed to pull image`` detail line targets the same image as
    the preceding ``docker pull <ref>`` command echo.

    Extracts the quoted image ref from *detail_line* using
    ``_CI_DOCKER_IMAGE_PULL_FAILURE_REF_PATTERN`` and checks that the same ref
    appears as a whitespace-delimited token in at least one ``docker pull``
    command echo found in ``lines[back_start:summary_index]``.  Returns False
    when the detail carries no quoted ref or when no matching command echo
    exists in the backward window — both cases indicate the detail cannot be
    confirmed as belonging to the same pull operation.
    """
    match = _CI_DOCKER_IMAGE_PULL_FAILURE_REF_PATTERN.search(detail_line)
    if match is None:
        return False
    image_ref = match.group(1)
    return any(
        _CI_DOCKER_PULL_COMMAND_MARKER in lines[k]
        and _CI_DOCKER_SELF_EVIDENT_PULL_FAILURE_MARKER not in lines[k]
        and image_ref in lines[k].split()
        for k in range(back_start, summary_index)
    )


def _evidence_line_is_permanent_pull_failure(index: int, lines: list[str]) -> bool:
    """Whether a Docker pull-failure evidence line represents a permanent error.

    Returns True when the evidence line itself, any adjacent daemon-error line
    within ``_CI_DOCKER_TIMEOUT_EVIDENCE_WINDOW`` *before* the evidence line, or
    (for ``docker pull failed`` summaries) any ``failed to pull image`` detail
    within the window *after* the summary contains a permanent pull-error phrase
    (access-denied, no-such-image, manifest-unknown, etc.).  Permanent errors
    cannot succeed on a retry, so their evidence lines must not anchor
    transient-timeout attribution.

    Backward daemon-error probe: Docker CLI always emits the daemon error before
    the ``docker pull failed`` summary for the same pull, so a daemon-error line
    appearing after the summary belongs to a different pull operation and must not
    influence permanence classification for this evidence line.

    Forward detail probe: Docker sometimes emits the ``docker pull failed``
    summary *before* the containerd/kubelet ``failed to pull image "<ref>":
    <permanent-reason>`` detail line.  When such a detail follows a summary
    within the window without an intervening new ``docker pull`` command echo,
    **and** its quoted image ref matches the ref from the preceding
    ``docker pull <ref>`` command echo, it belongs to the same pull and makes
    the summary permanent.  A detail for a *different* image (e.g. an
    unrelated kubelet event in the same step log) must not influence permanence
    classification for this pull.

    In both directions, if a ``docker pull`` command echo (not itself a failure
    summary) appears between the probe line and the evidence line, that echo
    marks a new pull invocation and the probe is excluded.
    """
    clean_current = re.sub(r'"[^"]*"', "", lines[index])
    if any(marker in clean_current for marker in _CI_DOCKER_PERMANENT_PULL_ERROR_MARKERS):
        return True
    start = max(0, index - _CI_DOCKER_TIMEOUT_EVIDENCE_WINDOW)
    # Backward probe: daemon error always precedes the "docker pull failed"
    # summary; a daemon error after the summary is from a different pull.
    # Restrict to "docker pull failed" summary lines only — when the evidence
    # line is itself a daemon-error timeout, a preceding daemon error for a
    # *different* image must not make it permanent (PRRT_kwDOSJAM6s6HsNGM).
    if _CI_DOCKER_SELF_EVIDENT_PULL_FAILURE_MARKER in lines[index] and any(
        _CI_DOCKER_DAEMON_ERROR_MARKER in lines[probe_index]
        and any(
            marker in re.sub(r'"[^"]*"', "", lines[probe_index])
            for marker in _CI_DOCKER_PERMANENT_PULL_ERROR_MARKERS
        )
        and not any(
            _CI_DOCKER_PULL_COMMAND_MARKER in lines[k]
            and _CI_DOCKER_SELF_EVIDENT_PULL_FAILURE_MARKER not in lines[k]
            for k in range(probe_index + 1, index)
        )
        for probe_index in range(start, index)
    ):
        return True
    # Forward probe: when the evidence line is a "docker pull failed" summary,
    # look forward for a "failed to pull image" detail with a permanent marker
    # that targets the same image ref as the preceding docker pull command echo.
    if _CI_DOCKER_SELF_EVIDENT_PULL_FAILURE_MARKER in lines[index]:
        end = min(len(lines), index + _CI_DOCKER_TIMEOUT_EVIDENCE_WINDOW + 1)
        # Narrow the backward ref-match search to the most recent docker pull
        # echo before the summary: starting from 0 would let a stale echo for
        # an earlier image pair with a detail for that image, wrongly marking
        # the current (different-image) pull as permanent (PRRT_kwDOSJAM6s6Hse5B).
        back_start = 0
        for k in range(index - 1, -1, -1):
            if (
                _CI_DOCKER_PULL_COMMAND_MARKER in lines[k]
                and _CI_DOCKER_SELF_EVIDENT_PULL_FAILURE_MARKER not in lines[k]
            ):
                back_start = k
                break
        return any(
            _CI_DOCKER_IMAGE_PULL_FAILURE_MARKER in lines[probe_index]
            and any(
                marker in re.sub(r'"[^"]*"', "", lines[probe_index])
                for marker in _CI_DOCKER_PERMANENT_PULL_ERROR_MARKERS
            )
            and not any(
                _CI_DOCKER_PULL_COMMAND_MARKER in lines[k]
                and _CI_DOCKER_SELF_EVIDENT_PULL_FAILURE_MARKER not in lines[k]
                for k in range(index + 1, probe_index)
            )
            and _forward_detail_ref_matches_pull(lines[probe_index], back_start, index, lines)
            for probe_index in range(index + 1, end)
        )
    return False


def _log_shows_docker_registry_timeout(log_text: str) -> bool:
    """Whether a generic network-timeout phrase is tied to a Docker pull failure.

    The phrases in ``_CI_DOCKER_REGISTRY_TIMEOUT_MARKERS`` are only transient
    when the timeout line is *part of* the Docker pull failure — on the same line
    as, or within ``_CI_DOCKER_TIMEOUT_EVIDENCE_WINDOW`` lines of, a Docker
    pull-*failure* line (a daemon error response, an explicit pull-failed message,
    or a ``failed to pull image`` line corroborated by nearby Docker pull
    context). Proximity to a bare ``docker pull`` *command* echo is not enough on
    its own: that echo precedes successful setup pulls too, so anchoring on it
    would still fire when a successful pull merely co-exists in the same
    ``--log-failed`` step as a real application/integration test timeout that must
    reach the repair agent. The ``docker pull`` echo only *corroborates* an
    explicit ``failed to pull image`` line — it never anchors on its own.

    The timeout phrase must also belong to the pull it is attributed to. A timeout
    that is part of an *uncorroborated* ``failed to pull image`` event is that
    kubelet/containerd application-image event's own error (a real deploy bug) —
    not the transient pull's — so it must not satisfy this check by sitting within
    the window of an unrelated Docker pull-failure evidence line (e.g. a service-
    container ``docker pull failed``). That holds whether the timeout sits *on* the
    ``failed to pull image`` line (``Failed to pull image "app": context deadline
    exceeded``) or is *wrapped onto a following line* of the same event (``Failed to
    pull image "app"`` then a bare ``context deadline exceeded`` on the next line) —
    the wrapped form carries no ``failed to pull image`` text of its own, so an
    on-line-only guard would let it be attributed to a nearby unrelated transient
    pull failure. Any timeout marker within ``_CI_DOCKER_TIMEOUT_EVIDENCE_WINDOW``
    lines of an uncorroborated ``failed to pull image`` line is therefore excluded
    as a timeout *source*, keeping a real application-image bug from being silently
    rerun by mere line proximity.

    That uncorroborated-proximity exclusion does *not* apply to a timeout line that
    is itself one of the Docker pull-*failure* evidence lines. A self-evident daemon
    registry timeout (``Error response from daemon: Get
    "https://registry-1.docker.io/v2/": context deadline exceeded``) is clear
    registry-flake evidence on its own, and an uncorroborated ``failed to pull
    image`` summary printed immediately before it (no double-quoted ref to
    corroborate, or a wrapped summary) must not drag that genuine timeout out of the
    transient set — otherwise a real registry flake would reach the repair agent
    instead of being rerun. Such an evidence line is exempt from the exclusion.
    """

    lines = log_text.lower().splitlines()
    # A ``docker pull failed ...`` *failure summary* line contains the ``docker
    # pull`` substring too, but it is not a ``docker pull <ref>`` *command echo*:
    # its ``split()`` tokens (``docker``/``pull``/``failed``/...) would let a
    # ``failed to pull image "docker"`` kubelet event (``docker`` is a real Docker
    # Hub image) match by token and be wrongly corroborated, dropping a real deploy
    # bug out of ``uncorroborated_image_pull_indexes``. Self-evident pull-failure
    # lines already anchor as their own evidence, so exclude them here.
    docker_pull_command_indexes = tuple(
        index
        for index, line in enumerate(lines)
        if _CI_DOCKER_PULL_COMMAND_MARKER in line
        and _CI_DOCKER_SELF_EVIDENT_PULL_FAILURE_MARKER not in line
    )
    evidence_line_indexes = [
        index
        for index, line in enumerate(lines)
        if _is_docker_pull_failure_line(
            index,
            line,
            lines,
            docker_pull_command_indexes,
        )
    ]
    if not evidence_line_indexes:
        return False
    evidence_line_set = set(evidence_line_indexes)
    # Filter out evidence lines that represent permanent (non-retryable) pull
    # errors — access-denied, no-such-image, manifest-unknown, etc.  A permanent
    # pull failure cannot succeed on a retry, so its proximity to an unrelated
    # ``context deadline exceeded`` must not trigger a silent rerun.  The
    # ``evidence_line_set`` above intentionally keeps ALL evidence lines (both
    # transient and permanent) so that the uncorroborated-proximity guard below
    # still works correctly for timeout lines that are themselves evidence lines.
    transient_evidence_line_indexes = [
        index
        for index in evidence_line_indexes
        if not _evidence_line_is_permanent_pull_failure(index, lines)
    ]
    if not transient_evidence_line_indexes:
        return False
    # The bare ``failed to pull image`` substring is too loose to mark a line as a
    # kubelet/containerd application-image event: an ordinary app log line such as
    # ``failed to pull image catalog from https://cdn`` contains the substring but
    # is not a pull *event* (it carries no ``"<ref>"``). Treating it as an
    # uncorroborated pull would let any genuine registry timeout within
    # ``_CI_DOCKER_TIMEOUT_EVIDENCE_WINDOW`` lines of it be excluded as that
    # "event's" own error and silently reported instead of rerun. Genuine
    # kubelet/containerd/Docker ``failed to pull image "<ref>"`` events always quote
    # the ref (the same form ``_image_pull_failure_is_corroborated`` keys on), so
    # require that quoted-ref shape here too.
    uncorroborated_image_pull_indexes = [
        index
        for index, line in enumerate(lines)
        if _CI_DOCKER_IMAGE_PULL_FAILURE_REF_PATTERN.search(line) and index not in evidence_line_set
    ]
    return any(
        any(marker in line for marker in _CI_DOCKER_REGISTRY_TIMEOUT_MARKERS)
        and (
            index in evidence_line_set
            or not any(
                abs(index - uncorroborated_index) <= _CI_DOCKER_TIMEOUT_EVIDENCE_WINDOW
                # When the uncorroborated pull precedes the timeout (the typical
                # kubelet header+wrapped-error ordering), an interleaved evidence
                # line cannot claim the timeout — it still belongs to the kubelet
                # event.  Only when the uncorroborated pull comes *after* the
                # timeout can an evidence line sitting strictly between them
                # plausibly attribute the timeout to the transient pull instead.
                and (
                    uncorroborated_index < index
                    or not any(
                        min(index, uncorroborated_index) < ev_idx < max(index, uncorroborated_index)
                        for ev_idx in evidence_line_set
                    )
                )
                for uncorroborated_index in uncorroborated_image_pull_indexes
            )
        )
        and any(
            abs(index - evidence_index) <= _CI_DOCKER_TIMEOUT_EVIDENCE_WINDOW
            for evidence_index in transient_evidence_line_indexes
        )
        for index, line in enumerate(lines)
    )
