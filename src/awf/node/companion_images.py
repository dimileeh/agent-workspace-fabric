"""Pre-build and cache managed companion images on the host daemon.

Companions build on the shared host Docker daemon (the control plane runs
``docker compose`` against the host socket), so an image built once and tagged
locally is reusable by every workspace via an ``image:`` reference -- no
registry is required. This builder derives a deterministic tag per
``(companion name, commit sha)``, builds it once (deduping concurrent dispatch
waves by sharing a single in-flight build task per tag and build budget), and
skips the build when the tag already exists. The tag also folds in a digest of
the resolved build inputs (``build_context`` and ``dockerfile``), so two
workspaces that request the same companion name at the same commit but with
different build definitions never collide on a tag and reuse the wrong image.

Coordination is in-process only: the single-node local Core runs one worker, so
sharing one :class:`asyncio.Task` per ``(tag, build budget)`` is sufficient. The
in-flight registry is keyed by both so a slower stack never inherits a faster
stack's shorter subprocess cap; the cached image is still keyed by ``tag`` alone,
so the cross-workspace cache hit is shared regardless of budget. The shared task
is dropped as soon as it finishes, so the registry only ever holds in-flight
builds: a failed build is retried by the next dispatch wave instead of being
cached as a permanent failure, and the registry cannot grow without bound. A
future multi-node deployment would need a registry plus a distributed lock; that
is intentionally out of scope here.
"""

from __future__ import annotations

import asyncio
import hashlib
import re

from awf.common.logging import get_logger
from awf.node.compose_manager import (
    COMPANION_IMAGE_MANAGED_LABEL,
    COMPANION_IMAGE_NAME_LABEL,
    ComposeManager,
    ComposeOperationError,
)

_log = get_logger(__name__)

_TAG_NAME_SANITIZE = re.compile(r"[^a-z0-9_.-]+")
_SHORT_SHA_LENGTH = 12
_BUILD_DIGEST_LENGTH = 12


def _companion_build_inputs_digest(build_context: str, dockerfile: str) -> str:
    """Return a short, stable digest of the repo-relative companion build inputs.

    A NUL separator keeps the digest unambiguous (e.g. ``("a", "b/c")`` and
    ``("a/b", "c")`` cannot collide).
    """
    payload = "\0".join((build_context, dockerfile)).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()[:_BUILD_DIGEST_LENGTH]


def companion_image_tag(
    name: str,
    commit_sha: str,
    *,
    build_context: str,
    dockerfile: str,
) -> str:
    """Return the deterministic local image tag for a companion build.

    The tag folds a digest of the build inputs (``build_context`` and
    ``dockerfile``) into the tag portion, so two workspaces that request the same
    companion ``name`` at the same ``commit_sha`` but with different build
    definitions resolve to distinct tags. Without this, the second launch would
    see the first build's tag via :meth:`ComposeManager.companion_image_exists`
    and reuse an image built from a different ``build:`` definition.

    Callers must pass the *repo-relative* build inputs (not the per-worktree
    absolute build context) so the tag stays stable across workspaces and the
    cross-workspace cache still hits for identical build definitions.
    """
    safe_name = _TAG_NAME_SANITIZE.sub("-", name.strip().lower()).strip("-._") or "companion"
    short_sha = commit_sha.strip().lower()[:_SHORT_SHA_LENGTH]
    build_digest = _companion_build_inputs_digest(build_context, dockerfile)
    return f"awf-companion-{safe_name}:{short_sha}-{build_digest}"


def companion_image_prune_command(retention_hours: int) -> list[str]:
    """Return the ``docker`` argv that prunes stale managed companion images.

    ``image prune`` never removes an image backing a live container, so images
    for active workspaces are protected automatically; only unreferenced
    companion builds older than the retention window are removed.

    ``--filter until=<h>h`` keys on the image's *creation* time, not when it was
    last used to launch a workspace -- Docker exposes no "last used" filter for
    ``image prune``. So a cached image built more than ``retention_hours`` ago can
    be pruned even though a workspace launched from it recently, as long as no
    container is currently running off it. For a *future* dispatch this is benign:
    it sees the tag is gone, rebuilds it once, and provisioning continues correctly
    -- the only cost is an unexpected cold build. An operator who observes a
    companion rebuilding sooner than the retention window seems to imply is seeing
    creation-age eviction of a stopped-but-not-destroyed workspace's image, not a
    bug.

    It is *not* harmless for a dispatch that is already in flight, though. Once
    :meth:`CompanionImageBuilder.ensure` resolves a cached tag, the stack renders
    that companion with ``image:`` + ``pull_policy: never`` and no inline
    ``build:`` fallback (the inline fallback only covers an ``ensure`` that returns
    ``None`` -- no resolvable commit or a build failure -- not a tag pruned after
    it resolves). A prune that races into the narrow window between ``ensure``'s
    ``companion_image_exists`` check and the subsequent ``docker compose up``
    removes the only local copy, so compose up fails with a missing-image
    :class:`~awf.node.compose_manager.ComposeOperationError` instead of rebuilding,
    and *that* workspace's provisioning fails (it is preserved as ``failed``; a
    later provisioning attempt rebuilds the now-missing tag). The window is small
    and requires no live container off the shared tag, but it is a real
    prune/dispatch race rather than a guaranteed cold build. Because the eviction
    self-corrects on the next build, callers do not retry compose up against the
    pinned image -- doing so would hide genuine compose failures behind a retry.
    """
    return [
        "docker",
        "image",
        "prune",
        "--all",
        "--force",
        "--filter",
        f"label={COMPANION_IMAGE_MANAGED_LABEL}=true",
        "--filter",
        f"until={retention_hours}h",
    ]


class CompanionImageBuilder:
    """Builds companion images once per ``(name, commit)`` and reuses the tag."""

    def __init__(self, compose: ComposeManager) -> None:
        """Store the compose manager used to run host-daemon docker commands."""
        self._compose = compose
        self._builds: dict[tuple[str, float], asyncio.Task[str | None]] = {}

    async def companion_image_exists(self, tag: str) -> bool:
        """Return whether ``tag`` still exists, preserving genuine probe errors.

        This launch-time check distinguishes a confirmed missing image from a
        Docker probe failure. The cache path's cheaper existence helper treats
        inspect failures as "not present" so it can build; point-of-use
        revalidation must not hide daemon errors behind an inline-build fallback.
        """
        try:
            await self._compose._docker_capture(  # noqa: SLF001
                ["image", "inspect", tag],
                operation="image inspect",
            )
        except ComposeOperationError as exc:
            if _is_missing_image_inspect_failure(exc):
                return False
            _log.warning(
                "companion_image.revalidate_failed",
                tag=tag,
                reason_code=exc.reason_code,
                stderr=exc.stderr[:1000],
            )
            raise
        return True

    async def ensure(
        self,
        *,
        name: str,
        commit_sha: str,
        build_context: str,
        dockerfile: str,
        relative_build_context: str,
        capture_timeout_seconds: float,
    ) -> str | None:
        """Return a ready-to-reference image tag, building it once if needed.

        ``build_context`` is the resolved (per-worktree absolute) path handed to
        ``docker build``; ``relative_build_context`` is the repo-relative build
        context from the companion spec. The cache key is derived from the
        repo-relative build inputs (``relative_build_context`` and the already
        worktree-stable ``dockerfile``) rather than the absolute path, so a build
        definition that varies ``build_context`` or ``dockerfile`` gets its own
        tag while identical definitions still reuse one image across workspaces.

        ``capture_timeout_seconds`` is the build's subprocess budget; callers pass
        the same effective compose-up cap the inline ``docker compose up`` build
        uses, so the cache pre-build can never time out earlier than the inline
        build it replaces. The in-flight registry is keyed by this budget too, so
        a waiter only shares a build started with the same cap and never inherits a
        shorter one -- the invariant holds for every waiter, not just the first.

        Concurrent dispatches for the same tag and budget share one in-flight build
        task, so the underlying ``docker build`` runs (and any failure is logged)
        exactly once per wave instead of once per waiter -- a broken companion can
        no longer block the worker queue with N sequential rebuild attempts. The
        shared task is dropped when it finishes, so the registry only holds
        in-flight builds: a success is re-checked cheaply by the next wave and a
        failure is retried, and the registry never grows without bound.

        Returns ``None`` when caching cannot be applied (no resolvable commit or
        a build failure); the caller falls back to an inline ``build:`` service
        so provisioning stays correct.
        """
        if not commit_sha.strip():
            return None
        tag = companion_image_tag(
            name,
            commit_sha,
            build_context=relative_build_context,
            dockerfile=dockerfile,
        )
        # Dedup is keyed by ``(tag, capture_timeout_seconds)`` rather than ``tag``
        # alone: concurrent dispatches share one in-flight build only when they
        # also share a build budget, so a stack with a larger effective
        # ``compose_up_timeout_seconds`` never inherits a shorter cap from whichever
        # caller happened to start the build first -- which would time the cache
        # pre-build out earlier than that stack's own inline ``docker compose up``
        # build would. Distinct budgets each get their own build; the resulting
        # image is still content-addressed by ``tag``, so a later dispatch under any
        # budget reuses the cached image via the existence check in _ensure_build.
        # No ``await`` between get and set, so registering the shared task is atomic
        # on the event loop: a concurrent wave with the same key awaits the one task
        # and builds once.
        key = (tag, capture_timeout_seconds)
        task = self._builds.get(key)
        if task is None:
            task = asyncio.create_task(
                self._ensure_build(
                    tag=tag,
                    name=name,
                    build_context=build_context,
                    dockerfile=dockerfile,
                    capture_timeout_seconds=capture_timeout_seconds,
                )
            )
            self._builds[key] = task
            # Drop the finished task either way: a failure must be retriable by
            # the next wave rather than cached forever, and a success need not
            # linger (the next wave re-checks the tag cheaply), so the registry
            # only ever holds in-flight builds.
            task.add_done_callback(lambda _task: self._builds.pop(key, None))
        # ``asyncio.shield`` isolates the shared build from any single waiter's
        # cancellation. Awaiting the Task directly would propagate a cancelled
        # waiter's ``CancelledError`` into the shared Task (an awaiter becomes the
        # Task's ``_fut_waiter``, so cancelling the waiter cancels the Task),
        # aborting the in-flight build for *every* other waiter in the wave rather
        # than just abandoning that one wait. Shielded, a cancelled waiter only
        # drops its own wait while the build runs on for the survivors.
        return await asyncio.shield(task)

    async def _ensure_build(
        self,
        *,
        tag: str,
        name: str,
        build_context: str,
        dockerfile: str,
        capture_timeout_seconds: float,
    ) -> str | None:
        """Reuse the tag when it already exists, otherwise build it once."""
        if await self._compose.companion_image_exists(tag):
            _log.info("companion_image.cache_hit", companion=name, tag=tag)
            return tag
        try:
            await self._compose.build_companion_image(
                tag=tag,
                build_context=build_context,
                dockerfile=dockerfile,
                labels={
                    COMPANION_IMAGE_MANAGED_LABEL: "true",
                    COMPANION_IMAGE_NAME_LABEL: name,
                },
                capture_timeout_seconds=capture_timeout_seconds,
            )
        except ComposeOperationError as exc:
            _log.warning(
                "companion_image.build_failed",
                companion=name,
                tag=tag,
                reason_code=exc.reason_code,
                stderr=exc.stderr[:1000],
            )
            return None
        _log.info("companion_image.built", companion=name, tag=tag)
        return tag


def _is_missing_image_inspect_failure(exc: ComposeOperationError) -> bool:
    """Return whether an ``image inspect`` failure confirms an absent tag."""
    if exc.reason_code != "COMPOSE_COMMAND_FAILED":
        return False
    detail = f"{exc.stderr}\n{exc.stdout}".lower()
    return "no such image" in detail or "not found" in detail
