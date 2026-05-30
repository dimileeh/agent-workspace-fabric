"""Pre-build and cache managed companion images on the host daemon.

Companions build on the shared host Docker daemon (the control plane runs
``docker compose`` against the host socket), so an image built once and tagged
locally is reusable by every workspace via an ``image:`` reference -- no
registry is required. This builder derives a deterministic tag per
``(companion name, commit sha)``, builds it once (deduping concurrent dispatch
waves with a per-tag lock), and skips the build when the tag already exists.

Coordination is in-process only: the single-node local Core runs one worker, so
an :class:`asyncio.Lock` per tag is sufficient. A future multi-node deployment
would need a registry plus a distributed lock; that is intentionally out of
scope here.
"""

from __future__ import annotations

import asyncio
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


def companion_image_tag(name: str, commit_sha: str) -> str:
    """Return the deterministic local image tag for a companion build."""
    safe_name = _TAG_NAME_SANITIZE.sub("-", name.strip().lower()).strip("-._") or "companion"
    short_sha = commit_sha.strip().lower()[:_SHORT_SHA_LENGTH]
    return f"awf-companion-{safe_name}:{short_sha}"


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
        self._locks: dict[str, asyncio.Lock] = {}

    def _lock_for(self, tag: str) -> asyncio.Lock:
        # No ``await`` between get and set, so this is atomic on the event loop:
        # concurrent provisions for the same tag share one lock and build once.
        lock = self._locks.get(tag)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[tag] = lock
        return lock

    async def ensure(
        self,
        *,
        name: str,
        commit_sha: str,
        build_context: str,
        dockerfile: str,
        capture_timeout_seconds: float,
    ) -> str | None:
        """Return a ready-to-reference image tag, building it once if needed.

        ``capture_timeout_seconds`` is the build's subprocess budget; callers pass
        the same effective compose-up cap the inline ``docker compose up`` build
        uses, so the cache pre-build can never time out earlier than the inline
        build it replaces.

        Returns ``None`` when caching cannot be applied (no resolvable commit or
        a build failure); the caller falls back to an inline ``build:`` service
        so provisioning stays correct.
        """
        if not commit_sha.strip():
            return None
        tag = companion_image_tag(name, commit_sha)
        async with self._lock_for(tag):
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
