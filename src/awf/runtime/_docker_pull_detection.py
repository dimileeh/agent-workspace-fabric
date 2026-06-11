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

# Docker Hub registry hosts that appear in daemon error URLs for Docker Hub image
# pulls.  Unqualified image refs (no explicit registry such as ``org/app:latest``
# or ``postgres:16``) resolve implicitly to Docker Hub; the daemon error URL for
# such a pull carries one of these hostnames.  Used in
# ``_image_ref_matches_daemon_url`` to prevent a hostless ``/v2/<repo>/manifests/``
# fragment from matching a permanent error URL from a different registry
# (e.g. ``ghcr.io``) that happens to host the same repo path.
_DOCKER_HUB_REGISTRY_HOSTS = ("registry-1.docker.io", "index.docker.io", "docker.io")

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
# Shell redirection / pipe metacharacters that may trail a ``docker pull <image>``
# command echo (e.g. ``docker pull img 2>&1``).  Tokens containing these must not
# be mistaken for the image ref when extracting it from the echo.
_SHELL_REDIRECTION_METACHAR_RE = re.compile(r"[><&|;]")
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
    ": denied",  # HTTP-style registry denial: 'Head "https://...": denied'
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
        0 < index - context_index <= _CI_DOCKER_TIMEOUT_EVIDENCE_WINDOW
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
    detail_line: str,
    back_start: int,
    summary_index: int,
    lines: list[str],
    stale_guard_image: str | None = None,
) -> bool:
    """Whether a ``failed to pull image`` detail line targets the same image as
    the preceding ``docker pull <ref>`` command echo, or whether no such echo
    is available (no-echo fallback).

    Extracts the quoted image ref from *detail_line* using
    ``_CI_DOCKER_IMAGE_PULL_FAILURE_REF_PATTERN`` and checks that the same ref
    appears as a whitespace-delimited token in at least one ``docker pull``
    command echo found in ``lines[back_start:summary_index]``.  When no pull
    command echo exists in that window (the wrapper/log stream did not echo the
    pull command), returns True — the caller's "no intervening pull" guard
    already ensures the detail is adjacent to the summary with no new pull
    started between them.  Returns False only when the detail carries no quoted
    ref, or when a pull echo is present but its image ref does not match.

    When *stale_guard_image* is provided, the no-echo fallback is constrained:
    instead of accepting any detail, the detail's image ref must be consistent
    with *stale_guard_image* (exact match or tagless form).  The caller sets
    this when the nearest pull echo precedes the evidence window and its pull
    did *not* succeed — in that case the current summary likely belongs to the
    stale pull, so an unrelated kubelet event for a different image must not
    make it permanent (PRRT_kwDOSJAM6s6Ht11n).
    """
    match = _CI_DOCKER_IMAGE_PULL_FAILURE_REF_PATTERN.search(detail_line)
    if match is None:
        return False
    image_ref = match.group(1)
    echo_found = False
    for k in range(back_start, summary_index):
        if (
            _CI_DOCKER_PULL_COMMAND_MARKER in lines[k]
            and _CI_DOCKER_SELF_EVIDENT_PULL_FAILURE_MARKER not in lines[k]
        ):
            echo_found = True
            if image_ref in lines[k].split():
                return True
    if echo_found:
        return False
    # No pull echo exists in the backward window: the wrapper/log stream did
    # not echo the command.  The caller's "no intervening pull" guard already
    # ensures the detail is adjacent to the summary.
    if stale_guard_image is not None:
        # A stale echo exists whose pull did not succeed — the summary likely
        # belongs to the stale pull; only accept if the detail targets the same
        # image (or its tagless/latest form) so unrelated kubelet events are
        # rejected.  A tagless stale pull (e.g. ``docker pull ghcr.io/org/app``)
        # defaults to ``:latest`` per Docker docs, so only a ``:latest``-tagged
        # detail (or the tagless daemon-error form) matches — accepting *any* tag
        # would cause ``ghcr.io/org/app:bad`` to match a stale ``ghcr.io/org/app``
        # pull and incorrectly mark a transient timeout as permanent
        # (PRRT_kwDOSJAM6s6Hujrw).
        return (
            image_ref == stale_guard_image
            or image_ref == _strip_image_tag(stale_guard_image)
            or image_ref == stale_guard_image + ":latest"
        )
    return True


def _image_ref_matches_daemon_url(image_ref: str, line: str) -> bool:
    """Whether a daemon error line's quoted URL is attributable to *image_ref*.

    Daemon auth/manifest errors commonly embed the image location as a registry
    distribution-API URL rather than quoting the image ref directly — e.g.
    ``Error response from daemon: Head
    "https://ghcr.io/v2/org/app/manifests/latest": denied``.  The image ref
    ``ghcr.io/org/app:latest`` is not a whitespace-delimited token on such a
    line, but the repository path ``org/app`` appears in the URL after ``/v2/``.
    This function strips the registry host prefix from the image ref, constructs
    the expected ``/v2/<repo>/manifests/<tag-or-digest>`` fragment, and checks
    whether it appears anywhere in *line*.  When the daemon URL contains a
    ``/manifests/`` path and the image ref carries an explicit tag or digest, both
    the repository and the manifest ref must match — a permanent error for
    ``ghcr.io/org/app:bad`` must not be attributed to a pull of
    ``ghcr.io/org/app:good`` (PRRT_kwDOSJAM6s6HtGA_).

    Non-manifest registry paths (blob URLs ``/v2/<repo>/blobs/<digest>``, tags
    list ``/v2/<repo>/tags/list``, etc.) are intentionally **not** matched.
    Blob URLs are content-addressed and carry no tag, so the same path appears
    for blobs of any tag in that repository.  A permanent ``not found`` blob
    error for a *different* image would share the same ``/v2/<repo>/`` prefix
    and would incorrectly be attributed to the in-flight pull, preventing a
    legitimate transient-timeout rerun (PRRT_kwDOSJAM6s6Huzsy).  Only manifest
    and token-service URLs carry enough information for safe attribution.

    Docker Hub official/library images (single-component names such as
    ``postgres`` or ``ubuntu``) are served under ``/v2/library/<name>/`` in
    daemon API URLs, not ``/v2/<name>/``, so both fragments are checked when
    the resolved repo path contains no slash.
    """
    # Strip any @digest suffix (e.g. @sha256:abc) before tag/registry
    # processing.  A digest colon would otherwise be mistaken for a tag
    # separator, leaving the algorithm suffix in repo_path.  Save the digest
    # so it can be used to narrow the manifests URL match.
    manifest_ref: str | None = None
    at_sign = image_ref.find("@")
    if at_sign != -1:
        manifest_ref = image_ref[at_sign + 1 :]  # e.g. "sha256:abc123"
        image_ref = image_ref[:at_sign]
    # Strip the image tag (last ":tag") without confusing a registry port
    # ("registry.host:5000/image:tag") with a tag separator.  A colon is a
    # tag separator only when nothing after it contains a "/" — a registry
    # port is always followed by a "/" (the image path component).  Save the
    # tag so it can be used to narrow the manifests URL match.
    colon = image_ref.rfind(":")
    if colon != -1 and "/" not in image_ref[colon:]:
        if manifest_ref is None:
            manifest_ref = image_ref[colon + 1 :]  # e.g. "latest"
        ref_no_tag = image_ref[:colon]
    else:
        ref_no_tag = image_ref
    slash = ref_no_tag.find("/")
    # The first path component is a registry host when it contains a "."
    # (dotted hostname), a ":" (host:port, e.g. localhost:5000), or is the
    # literal "localhost" — matching Docker's own name-parsing convention.
    _host = ref_no_tag[:slash] if slash != -1 else ""
    _is_registry_host = "." in _host or ":" in _host or _host == "localhost"
    repo_path = ref_no_tag[slash + 1 :] if slash != -1 and _is_registry_host else ref_no_tag
    if not repo_path:
        return False
    manifests_prefix = f"/v2/{repo_path}/manifests/"
    # Docker Hub alias hosts (docker.io, index.docker.io) are resolved by the
    # daemon to registry-1.docker.io, so daemon URLs always carry the resolved
    # host rather than the alias.  Treat them like unqualified Docker Hub refs:
    # use the unscoped path and guard with a Docker Hub registry host in the
    # line (PRRT_kwDOSJAM6s6Htl06).
    _is_docker_hub_alias = _is_registry_host and _host in _DOCKER_HUB_REGISTRY_HOSTS
    # When the image ref carries an explicit non-Docker-Hub registry host, scope
    # the daemon URL check to that host — a permanent error from a different
    # registry for the same repo path and tag must not be attributed to an
    # in-flight pull from the expected registry (PRRT_kwDOSJAM6s6HtKzI).
    # Use "//<host>" as the URL-boundary delimiter so that a hostname that is a
    # suffix of another host (e.g. "registry.example.com" inside
    # "prod.registry.example.com") does not falsely match.  In daemon URLs the
    # registry host is always preceded by "://" (PRRT_kwDOSJAM6s6HtZng).
    url_manifests_prefix = (
        f"//{_host}{manifests_prefix}"
        if _is_registry_host and not _is_docker_hub_alias
        else manifests_prefix
    )
    if url_manifests_prefix in line:
        # Daemon URL is a manifest request; require tag/digest to match so
        # same-repo but different-tag errors are not conflated.  When no
        # explicit tag/digest is present, Docker defaults to "latest"
        # (https://docs.docker.com/reference/cli/docker/image/pull/), so use
        # that as the effective ref rather than accepting any manifest tag.
        #
        # For unqualified Docker Hub refs (no explicit registry host) and for
        # Docker Hub alias refs, the /v2/<repo>/manifests/ fragment is not
        # registry-scoped — the same path appears in daemon URLs from any
        # registry hosting that repo path.  Require a known Docker Hub registry
        # host in the line so a permanent error from a different registry (e.g.
        # ghcr.io) for the same path is not attributed to an in-flight Docker
        # Hub pull (PRRT_kwDOSJAM6s6HtNI4, PRRT_kwDOSJAM6s6Htl06).
        if (not _is_registry_host or _is_docker_hub_alias) and not any(
            f"//{h}/" in line for h in _DOCKER_HUB_REGISTRY_HOSTS
        ):
            return False
        effective_ref = manifest_ref if manifest_ref is not None else "latest"
        fragment = f"{url_manifests_prefix}{effective_ref}"
        idx = line.find(fragment)
        if idx == -1:
            return False
        after = idx + len(fragment)
        # Require a URL/token boundary after the ref so that a tag that is a
        # strict prefix of another (e.g. "v1" vs "v10") does not falsely match
        # (PRRT_kwDOSJAM6s6HuMAx).
        return after >= len(line) or not (line[after].isalnum() or line[after] in "._-")
    # Docker Hub library images (e.g. "postgres", "ubuntu") appear in daemon
    # URLs as /v2/library/<name>/ rather than /v2/<name>/.  Apply the same
    # Docker Hub registry-host guard as the non-library branches above: for
    # unqualified Docker Hub refs, require a known Docker Hub host in the line
    # so a permanent error from a different registry (e.g. ghcr.io) that
    # happens to embed the same library path is not attributed to an in-flight
    # Docker Hub pull (PRRT_kwDOSJAM6s6HtQiD).
    # The library/ URL form is a Docker Hub convention — only apply it for
    # unqualified refs (no explicit host) or refs that name a Docker Hub host
    # explicitly.  An explicit non-Docker-Hub ref such as ``ghcr.io/postgres:16``
    # has a single-component repo path just like an unqualified library image, but
    # its daemon URLs never contain ``/v2/library/<name>/`` — that path belongs to
    # Docker Hub.  Without this guard a nearby Docker Hub denial for
    # ``registry-1.docker.io/v2/library/postgres/manifests/16`` would be
    # incorrectly attributed to a ``ghcr.io/postgres:16`` pull, suppressing a
    # legitimate transient-timeout rerun (PRRT_kwDOSJAM6s6HtWjC).
    if "/" not in repo_path and (not _is_registry_host or _host in _DOCKER_HUB_REGISTRY_HOSTS):
        lib_prefix = f"/v2/library/{repo_path}/manifests/"
        if lib_prefix in line:
            # Apply the Docker Hub host guard for unqualified refs AND for explicit
            # Docker Hub alias hosts (docker.io / registry-1.docker.io / index.docker.io).
            # When the ref has an explicit Docker Hub alias host the daemon resolves it
            # to registry-1.docker.io, so the line will carry that host — not the alias.
            # Scoping to a Docker Hub host prevents a non-Docker-Hub URL (e.g.
            # ghcr.io/v2/library/<name>/...) from being incorrectly attributed to a
            # docker.io pull (PRRT_kwDOSJAM6s6HtZne).
            if (not _is_registry_host or _host in _DOCKER_HUB_REGISTRY_HOSTS) and not any(
                f"//{h}/" in line for h in _DOCKER_HUB_REGISTRY_HOSTS
            ):
                return False
            effective_ref = manifest_ref if manifest_ref is not None else "latest"
            fragment = f"{lib_prefix}{effective_ref}"
            idx = line.find(fragment)
            if idx == -1:
                return False
            after = idx + len(fragment)
            # Require a URL boundary after the ref so a tag that is a strict
            # prefix of another (e.g. "v1" vs "v10") does not falsely match
            # (PRRT_kwDOSJAM6s6HubRs).
            return after >= len(line) or not (line[after].isalnum() or line[after] in "._-")
    # Token-service endpoint URLs embed the repository in the OAuth scope
    # parameter as scope=repository%3A<repo>%3A<actions> (URL-encoded from
    # scope=repository:<repo>:<actions>).  A permanent auth failure at the
    # token service — e.g. ``Get
    # "https://ghcr.io/token?scope=repository%3Aorg%2Fapp%3Apull": denied``
    # — carries no /v2/ manifest path and is not recognised by the checks
    # above, so the permanence probe fails to attribute it to the in-flight
    # pull and the adjacent transient marker is incorrectly left non-permanent.
    # URL percent-encoding uses uppercase hex by convention (RFC 3986), but the
    # log text is lowercased before splitting (see _log_shows_docker_registry_timeout),
    # so the encoded sequences arrive as lowercase: %3a (:) and %2f (/).
    # Docker also emits the scope unencoded in some contexts (e.g. timeout
    # fixtures use scope=repository:org/app:pull without percent-encoding).
    # Match both forms so a permanent denial with either encoding is attributed
    # to the in-flight pull (PRRT_kwDOSJAM6s6HuFH6).  Require ``pull`` in the
    # action part so a push-only auth error for the same repo is not mistaken
    # for a pull failure (PRRT_kwDOSJAM6s6Hu-r8).
    encoded_repo = repo_path.replace("/", "%2f")
    token_scope = f"scope=repository%3a{encoded_repo}%3apull"
    unencoded_token_scope = f"scope=repository:{repo_path}:pull"
    if token_scope in line or unencoded_token_scope in line:
        if not _is_registry_host:
            # Unqualified Docker Hub ref: require a Docker Hub auth or registry
            # host in the token URL so an error from a different registry is
            # not attributed to a Docker Hub pull (mirrors PRRT_kwDOSJAM6s6HtNI4).
            # Use the "//<host>/" URL-boundary delimiter (as elsewhere in this
            # file, PRRT_kwDOSJAM6s6HtfLR) so a path-only "auth.docker.io" — e.g.
            # "https://evil/auth.docker.io/token" — does not falsely match.
            return "//auth.docker.io/" in line or any(
                f"//{h}/" in line for h in _DOCKER_HUB_REGISTRY_HOSTS
            )
        # Docker Hub registry hosts use auth.docker.io as their token service,
        # not the registry host itself — accept either.  Match on the
        # "//<host>/" URL boundary (PRRT_kwDOSJAM6s6HtfLR) so a path-only
        # "auth.docker.io" does not falsely match.
        if _host in _DOCKER_HUB_REGISTRY_HOSTS:
            return "//auth.docker.io/" in line or f"//{_host}/" in line
        # Use "//<host>/" as the URL-boundary delimiter so a hostname that is a
        # suffix of another host (e.g. "registry.example.com" inside
        # "prod.registry.example.com") does not falsely match.  Token-service
        # URLs always carry a "/" after the host ("/<path>?<query>"), so the
        # trailing "/" is always present (PRRT_kwDOSJAM6s6HtfLR).
        return f"//{_host}/" in line
    # Docker Hub library images (single-word names like "postgres") appear in
    # token requests as scope=repository%3alibrary%2f<name>%3a rather than
    # scope=repository%3a<name>%3a — mirrors the /v2/library/ treatment above.
    # Same Docker Hub scoping as the manifest URL block: skip this for explicit
    # non-Docker-Hub refs so a Docker Hub token denial is not attributed to a
    # pull of e.g. ``ghcr.io/postgres:16`` (PRRT_kwDOSJAM6s6HtWjC).
    # Match both encoded and unencoded scope forms (PRRT_kwDOSJAM6s6HuFH6).
    if "/" not in repo_path and (not _is_registry_host or _host in _DOCKER_HUB_REGISTRY_HOSTS):
        lib_token_scope = f"scope=repository%3alibrary%2f{repo_path}%3apull"
        unencoded_lib_token_scope = f"scope=repository:library/{repo_path}:pull"
        if lib_token_scope in line or unencoded_lib_token_scope in line:
            # Boundary-anchored host match ("//auth.docker.io/") so a path-only
            # "auth.docker.io" does not falsely match (PRRT_kwDOSJAM6s6HtfLR).
            return "//auth.docker.io/" in line or any(
                f"//{h}/" in line for h in _DOCKER_HUB_REGISTRY_HOSTS
            )
    return False


def _strip_image_tag(image_ref: str) -> str:
    """Return *image_ref* without its tag or digest suffix, for tagless daemon error matching.

    Docker access-denied errors commonly omit the tag: ``pull access denied for
    ghcr.io/org/app, repository does not exist or may require 'docker login':
    denied``.  Stripping the tag from the pull-echo image ref lets the token
    comparison match the tagless ref that appears in such daemon errors even when
    the echo carries an explicit tag like ``:latest``.  When the ref has no tag,
    returns it unchanged so callers need not special-case the tagless case.

    Digest-pinned refs (e.g. ``ghcr.io/org/app@sha256:abc123``) are fully
    stripped to the bare repository name — leaving ``@sha256`` in the result
    would prevent the tagless daemon denial token from matching
    (PRRT_kwDOSJAM6s6HuBR5).
    """
    at = image_ref.find("@")
    if at != -1:
        return image_ref[:at]
    colon = image_ref.rfind(":")
    if colon != -1 and "/" not in image_ref[colon:]:
        return image_ref[:colon]
    return image_ref


def _evidence_line_is_permanent_pull_failure(index: int, lines: list[str]) -> bool:
    """Whether a Docker pull-failure evidence line represents a permanent error.

    Returns True when the evidence line itself, any adjacent daemon-error line
    within ``_CI_DOCKER_TIMEOUT_EVIDENCE_WINDOW`` *before* the evidence line, or
    (for ``docker pull failed`` summaries) any ``failed to pull image`` detail
    within the window in *either direction* around the summary contains a permanent
    pull-error phrase (access-denied, no-such-image, manifest-unknown, etc.).
    Permanent errors cannot succeed on a retry, so their evidence lines must not
    anchor transient-timeout attribution.

    Backward daemon-error probe: Docker CLI always emits the daemon error before
    the ``docker pull failed`` summary for the same pull, so a daemon-error line
    appearing after the summary belongs to a different pull operation and must not
    influence permanence classification for this evidence line.  When the preceding
    pull echo is known, the daemon error must also carry the same image ref — an
    unrelated daemon error for a different image (e.g. an interleaved kubelet event)
    must not misattribute permanence to this summary.

    Backward detail probe: some log streams emit the containerd/kubelet
    ``failed to pull image "<ref>": <permanent-reason>`` detail *before* the
    ``docker pull failed`` summary (opposite of the typical ordering).  A permanent
    detail that precedes the summary without an intervening new ``docker pull``
    command echo belongs to the same pull and makes it non-retryable
    (PRRT_kwDOSJAM6s6HvFgf).

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
    # Both backward and forward daemon probes are restricted to "docker pull
    # failed" summary lines.  Extract the preceding pull echo and image ref
    # once so both probes can check image identity.
    if _CI_DOCKER_SELF_EVIDENT_PULL_FAILURE_MARKER in lines[index]:
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
        # Extract the image ref from the most-recent preceding pull echo so
        # both backward and forward daemon probes can confirm a daemon error
        # belongs to this pull.  When the preceding echo is known, a daemon
        # error for a *different* image must not influence permanence
        # classification for this summary
        # (PRRT_kwDOSJAM6s6Hr82p, PRRT_kwDOSJAM6s6Hs7GB).
        preceding_pull_image: str | None = None
        if (
            back_start
            >= start  # stale echo before the evidence window must not constrain image matching (PRRT_kwDOSJAM6s6HtCmc)
            and _CI_DOCKER_PULL_COMMAND_MARKER in lines[back_start]
            and _CI_DOCKER_SELF_EVIDENT_PULL_FAILURE_MARKER not in lines[back_start]
        ):
            pull_tokens = lines[back_start].split()
            try:
                pull_idx = next(i for i, t in enumerate(pull_tokens) if t == "pull")
                # Skip option flags (e.g. --quiet) and their values (e.g.
                # --platform linux/amd64) that may precede the image ref; the
                # image is always the last positional argument to docker pull.
                # Truncate at the first shell redirection / pipe metacharacter so
                # that e.g. "docker pull img 2>&1" does not store "2>&1" as the
                # image ref (PRRT_kwDOSJAM6s6HuqsM).
                _rel = pull_tokens[pull_idx + 1 :]
                _cut = next(
                    (i for i, t in enumerate(_rel) if _SHELL_REDIRECTION_METACHAR_RE.search(t)),
                    None,
                )
                non_flags = [
                    t for t in (_rel[:_cut] if _cut is not None else _rel) if not t.startswith("-")
                ]
                if non_flags:
                    preceding_pull_image = non_flags[-1]
            except StopIteration:
                pass
        # When the echo is stale (before the evidence window), preceding_pull_image
        # is left None so daemon probes are not gated on the stale image identity
        # (PRRT_kwDOSJAM6s6HtCmc).  However, if the stale pull did NOT succeed we
        # still need to constrain the forward detail probe's no-echo fallback so an
        # unrelated kubelet event cannot misclassify the transient timeout as
        # permanent (PRRT_kwDOSJAM6s6Ht11n).
        stale_guard_image: str | None = None
        if (
            back_start < start
            and _CI_DOCKER_PULL_COMMAND_MARKER in lines[back_start]
            and _CI_DOCKER_SELF_EVIDENT_PULL_FAILURE_MARKER not in lines[back_start]
        ):
            stale_tokens = lines[back_start].split()
            try:
                stale_pull_idx = next(i for i, t in enumerate(stale_tokens) if t == "pull")
                _stale_rel = stale_tokens[stale_pull_idx + 1 :]
                _stale_cut = next(
                    (
                        i
                        for i, t in enumerate(_stale_rel)
                        if _SHELL_REDIRECTION_METACHAR_RE.search(t)
                    ),
                    None,
                )
                stale_non_flags = [
                    t
                    for t in (_stale_rel[:_stale_cut] if _stale_cut is not None else _stale_rel)
                    if not t.startswith("-")
                ]
                if stale_non_flags:
                    _stale_img = stale_non_flags[-1]
                    # The stale guard constrains the no-echo fallback only when
                    # the current summary is plausibly the stale pull's own
                    # summary.  When a prior "docker pull failed" line already
                    # appeared between the stale echo and the current summary,
                    # the stale pull is already accounted for — the current
                    # summary belongs to a different pull (a different image),
                    # so gating on the stale image ref would reject the
                    # permanent detail for the actual failing image instead
                    # (PRRT_kwDOSJAM6s6HuLfZ).
                    if not _docker_pull_command_succeeded(
                        _stale_img, back_start, index, lines
                    ) and not any(
                        _CI_DOCKER_SELF_EVIDENT_PULL_FAILURE_MARKER in lines[k]
                        for k in range(back_start + 1, index)
                    ):
                        stale_guard_image = _stale_img
            except StopIteration:
                pass
        # Backward probe: daemon error always precedes the "docker pull failed"
        # summary for the same pull; a daemon error after the summary is from a
        # different pull.  Restrict to "docker pull failed" summary lines only —
        # when the evidence line is itself a daemon-error timeout, a preceding
        # daemon error for a *different* image must not make it permanent
        # (PRRT_kwDOSJAM6s6HsNGM).  Require image-identity match when the
        # preceding pull echo is known so an unrelated daemon error for a
        # different image (e.g. an interleaved kubelet event) does not
        # misattribute permanence to this summary (PRRT_kwDOSJAM6s6Hs7GB).
        if any(
            _CI_DOCKER_DAEMON_ERROR_MARKER in lines[probe_index]
            and any(
                marker in re.sub(r'"[^"]*"', "", lines[probe_index])
                for marker in _CI_DOCKER_PERMANENT_PULL_ERROR_MARKERS
            )
            and (
                preceding_pull_image is None
                or any(t.rstrip(",.;:") == preceding_pull_image for t in lines[probe_index].split())
                or any(
                    t.rstrip(",.;:") == _strip_image_tag(preceding_pull_image)
                    for t in lines[probe_index].split()
                )
                or _image_ref_matches_daemon_url(preceding_pull_image, lines[probe_index])
            )
            and not any(
                _CI_DOCKER_PULL_COMMAND_MARKER in lines[k]
                and _CI_DOCKER_SELF_EVIDENT_PULL_FAILURE_MARKER not in lines[k]
                for k in range(probe_index + 1, index)
            )
            for probe_index in range(start, index)
        ):
            return True
        # Backward detail probe: a ``failed to pull image "<ref>": <reason>``
        # detail with a permanent marker that appears *before* the "docker pull
        # failed" summary belongs to the same pull and makes it non-retryable.
        # Reuse ``_forward_detail_ref_matches_pull`` for image-ref matching —
        # it searches ``lines[back_start:index]`` for a pull echo, which covers
        # the backward window (PRRT_kwDOSJAM6s6HvFgf).
        if any(
            _CI_DOCKER_IMAGE_PULL_FAILURE_MARKER in lines[probe_index]
            and any(
                marker in re.sub(r'"[^"]*"', "", lines[probe_index])
                for marker in _CI_DOCKER_PERMANENT_PULL_ERROR_MARKERS
            )
            and not any(
                _CI_DOCKER_PULL_COMMAND_MARKER in lines[k]
                and _CI_DOCKER_SELF_EVIDENT_PULL_FAILURE_MARKER not in lines[k]
                for k in range(probe_index + 1, index)
            )
            and _forward_detail_ref_matches_pull(
                lines[probe_index],
                back_start,
                index,
                lines,
                stale_guard_image,
            )
            for probe_index in range(start, index)
        ):
            return True
        # Forward probe: look forward for a daemon permanent error or a
        # "failed to pull image" detail with a permanent marker that belongs
        # to this pull.
        end = min(len(lines), index + _CI_DOCKER_TIMEOUT_EVIDENCE_WINDOW + 1)
        # Forward daemon probe: some log streams emit the summary before the
        # daemon error line (opposite of the typical CLI ordering). A daemon
        # permanent error appearing after the summary and before any new pull
        # echo belongs to this pull and makes it non-retryable.  When a
        # preceding pull echo is known, require the daemon error to carry the
        # same image ref — an error for a different image must not be attributed
        # to this summary (PRRT_kwDOSJAM6s6Hr82p).
        if any(
            _CI_DOCKER_DAEMON_ERROR_MARKER in lines[probe_index]
            and any(
                marker in re.sub(r'"[^"]*"', "", lines[probe_index])
                for marker in _CI_DOCKER_PERMANENT_PULL_ERROR_MARKERS
            )
            and (
                preceding_pull_image is None
                or any(t.rstrip(",.;:") == preceding_pull_image for t in lines[probe_index].split())
                or any(
                    t.rstrip(",.;:") == _strip_image_tag(preceding_pull_image)
                    for t in lines[probe_index].split()
                )
                or _image_ref_matches_daemon_url(preceding_pull_image, lines[probe_index])
            )
            and not any(
                _CI_DOCKER_PULL_COMMAND_MARKER in lines[k]
                and _CI_DOCKER_SELF_EVIDENT_PULL_FAILURE_MARKER not in lines[k]
                for k in range(index + 1, probe_index)
            )
            for probe_index in range(index + 1, end)
        ):
            return True
        # Forward detail probe: "failed to pull image" with permanent markers,
        # same-ref as the preceding pull echo.
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
            and _forward_detail_ref_matches_pull(
                lines[probe_index],
                max(
                    back_start, start
                ),  # stale echo before the evidence window must not gate the detail match (PRRT_kwDOSJAM6s6HtwnG)
                index,
                lines,
                stale_guard_image,  # constrain no-echo fallback when stale pull did not succeed (PRRT_kwDOSJAM6s6Ht11n)
            )
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
