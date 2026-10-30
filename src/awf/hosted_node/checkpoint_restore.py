"""Verify restored manifest bytes and the actual cut before closed restore boot.

Authority and storage mapping come from the non-rewound controller. This read-only
helper neither discovers that authority nor turns a local manifest into one.
"""

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import asdict
from datetime import datetime
from typing import Any

from awf.hosted_node.checkpoint_cut import (
    CheckpointRoots,
    HeldConnection,
    validate_checkpoint_cut,
)
from awf.hosted_node.identity import MaterialDeniedError
from awf.hosted_node.restore_quarantine import evidence_fresh
from awf.hosted_node.restore_schemas import RestoreBinding, RestoreBootEvidence

MANIFEST_FIELDS = frozenset(
    (
        "schema_version",
        "operation",
        "request_sha256",
        "checkpoint_marker",
        "identity",
        "maintenance_generation",
        "disk_ref",
        "source_disk_id",
        "filesystem_uuid",
        "database_ref",
        "release",
        "ownership_sha256",
        "schema_sha256",
        "producers_sha256",
        "files",
        "marker_observed_at",
    )
)


def require(condition: bool, reason: str) -> None:
    if not condition:
        raise MaterialDeniedError(reason)


async def validate_restored_cut(
    connection: HeldConnection,
    manifest_bytes: bytes,
    authority: RestoreBootEvidence,
    pinned_roots: CheckpointRoots,
    pinned_release: Mapping[str, Any],
    monotonic_deadline: float,
) -> RestoreBootEvidence:
    """Return unchanged authority only after exact, bounded local verification.

    The caller owns the full-schema barrier throughout this read, then releases it
    while keeping physical writers excluded through boot. ``pinned_release`` is trusted release
    input, not extracted from the manifest. Provider coordinates remain opaque here.
    """
    try:
        binding = RestoreBinding.model_validate(authority.binding.model_dump())
        require(evidence_fresh(authority.expires_at), "RESTORE_AUTHORITY_EXPIRED")
        require(0 < len(manifest_bytes) <= 65536, "RESTORE_MANIFEST_INVALID")
        require(
            hashlib.sha256(manifest_bytes).hexdigest() == binding.manifest_sha256,
            "RESTORE_MANIFEST_MISMATCH",
        )
        manifest = json.loads(manifest_bytes)
        require(
            isinstance(manifest, dict) and manifest.keys() == MANIFEST_FIELDS,
            "RESTORE_MANIFEST_INVALID",
        )
        canonical = (
            json.dumps(manifest, sort_keys=True, separators=(",", ":"), ensure_ascii=True) + "\n"
        ).encode()
        require(canonical == manifest_bytes, "RESTORE_MANIFEST_INVALID")
        marker = binding.checkpoint
        require(
            type(manifest["schema_version"]) is int
            and manifest["schema_version"] == 1
            and manifest["operation"] == marker.checkpoint_id
            and manifest["request_sha256"] == marker.request_sha256
            and manifest["checkpoint_marker"] == marker.model_dump(mode="json")
            and manifest["identity"] == marker.identity.model_dump()
            and type(manifest["maintenance_generation"]) is int
            and manifest["maintenance_generation"] == marker.maintenance_generation
            and manifest["disk_ref"] == marker.disk_ref
            and manifest["database_ref"] == marker.database_ref,
            "RESTORE_MARKER_MISMATCH",
        )
        require(
            manifest["release"] == dict(pinned_release)
            and pinned_release["core_image"] == pinned_roots.image_digest
            and re.fullmatch(r"[0-9a-f]{40}", pinned_release["core_revision"]) is not None
            and pinned_release["schema_revision"] == marker.schema_revision
            and pinned_release["schema_sha256"] == pinned_roots.schema_sha256
            and manifest["schema_sha256"] == binding.schema_sha256 == pinned_roots.schema_sha256,
            "RESTORE_RELEASE_MISMATCH",
        )
        observed = datetime.fromisoformat(manifest["marker_observed_at"])
        require(
            observed.tzinfo is not None and marker.prepared_at <= observed <= binding.point_in_time,
            "RESTORE_CUT_TIME_MISMATCH",
        )
        proof = await validate_checkpoint_cut(connection, marker, pinned_roots, monotonic_deadline)
        require(
            manifest["files"] == json.loads(json.dumps(asdict(proof)))
            and proof.files_sha256 == binding.files_sha256,
            "RESTORE_FILES_MISMATCH",
        )
        require(evidence_fresh(authority.expires_at), "RESTORE_AUTHORITY_EXPIRED")
        return authority
    except MaterialDeniedError:
        raise
    except (ValueError, KeyError, TypeError, RecursionError):
        raise MaterialDeniedError("RESTORE_MANIFEST_INVALID") from None
