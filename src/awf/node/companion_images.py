"""Pre-build and cache managed companion images on the host daemon.

Companions build on the shared host Docker daemon (the control plane runs
``docker compose`` against the host socket), so an image built once and tagged
locally is reusable by every workspace via an ``image:`` reference -- no
registry is required. This builder derives a deterministic tag per
``(companion name, commit sha)``, builds it once (deduping concurrent dispatch
waves by sharing a single in-flight build task per tag), and skips the build
when the tag already exists. The tag also folds in a digest of the resolved
build inputs (``build_context`` and ``dockerfile``), so two workspaces that
request the same companion name at the same commit but with different build
definitions never collide on a tag and reuse the wrong image.

Coordination is in-process only: the single-node local Core runs one worker, so
sharing one :class:`asyncio.Task` per tag is sufficient. The shared task is
dropped as soon as it finishes, so the registry only ever holds in-flight
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
        self._builds: dict[str, asyncio.Task[str | None]] = {}

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
        build it replaces.

        Concurrent dispatches for the same tag share one in-flight build task, so
        the underlying ``docker build`` runs (and any failure is logged) exactly
        once per wave instead of once per waiter -- a broken companion can no
        longer block the worker queue with N sequential rebuild attempts. The
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
        # No ``await`` between get and set, so registering the shared task is
        # atomic on the event loop: a concurrent wave for the same tag awaits the
        # one task and builds once.
        task = self._builds.get(tag)
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
            self._builds[tag] = task
            # Drop the finished task either way: a failure must be retriable by
            # the next wave rather than cached forever, and a success need not
            # linger (the next wave re-checks the tag cheaply), so the registry
            # only ever holds in-flight builds.
            task.add_done_callback(lambda _task: self._builds.pop(tag, None))
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
