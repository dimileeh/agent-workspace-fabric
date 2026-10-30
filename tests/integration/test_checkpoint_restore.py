"""Restored cut validation uses actual held PostgreSQL and managed file bytes."""

import hashlib
import json
import time
from dataclasses import asdict, replace
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from awf.hosted_node.checkpoint_cut import validate_checkpoint_cut
from awf.hosted_node.identity import MaterialDeniedError
from awf.hosted_node.restore_schemas import RestoreBinding, RestoreBootEvidence
from tests.integration.test_checkpoint_cut import cut
from tests.integration.test_checkpoint_marker import schema_revision as schema_revision
from tests.integration.test_node_idle_gate import node as node
from tests.integration.test_node_maintenance import ready_node as ready_node


def encoded(value):
    return (
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True) + "\n"
    ).encode()


async def restored(connection, marker, roots):
    proof = await validate_checkpoint_cut(connection, marker, roots, time.monotonic() + 5)
    release = {
        "core_image": roots.image_digest,
        "core_revision": "1" * 40,
        "schema_revision": marker.schema_revision,
        "schema_sha256": roots.schema_sha256,
        "host_payload_sha256": "2" * 64,
        "preparation_sha256": "3" * 64,
    }
    manifest = {
        "schema_version": 1,
        "operation": marker.checkpoint_id,
        "request_sha256": marker.request_sha256,
        "checkpoint_marker": marker.model_dump(mode="json"),
        "identity": marker.identity.model_dump(),
        "maintenance_generation": marker.maintenance_generation,
        "disk_ref": marker.disk_ref,
        "source_disk_id": "synthetic-disk-id",
        "filesystem_uuid": str(uuid4()),
        "database_ref": marker.database_ref,
        "release": release,
        "ownership_sha256": "4" * 64,
        "schema_sha256": roots.schema_sha256,
        "producers_sha256": "5" * 64,
        "files": asdict(proof),
        "marker_observed_at": datetime.now(UTC).isoformat(),
    }
    raw = encoded(manifest)
    binding = RestoreBinding(
        restore_id=str(uuid4()),
        checkpoint=marker,
        manifest_sha256=hashlib.sha256(raw).hexdigest(),
        files_sha256=proof.files_sha256,
        schema_sha256=roots.schema_sha256,
        point_in_time=datetime.now(UTC),
        storage_mapping_sha256="6" * 64,
        fenced_identity=marker.identity.model_copy(update={"boot_epoch": 9}),
        candidate_incarnation="candidate",
        candidate_recovery_generation=marker.identity.recovery_generation + 1,
        boot_epoch_floor=9,
        target_disk_ref="restored-disk",
        target_database_ref="restored-database",
    )
    authority = RestoreBootEvidence(binding, datetime.now(UTC) + timedelta(seconds=10))
    return manifest, raw, release, authority


async def test_actual_restored_cut_preserves_current_authority_without_extending_expiry(
    ready_node, tmp_path
):
    from awf.hosted_node.checkpoint_restore import validate_restored_cut

    async with cut(ready_node, tmp_path) as (connection, marker, roots, path):
        _, raw, release, authority = await restored(connection, marker, roots)
        before = path.read_bytes(), path.stat().st_mtime_ns
        accepted = await validate_restored_cut(
            connection, raw, authority, roots, release, time.monotonic() + 5
        )
        assert accepted is authority
        assert accepted.expires_at == authority.expires_at
        assert accepted.binding.boot_epoch_floor == 9
        assert accepted.binding.target_database_ref == "restored-database"
        assert (path.read_bytes(), path.stat().st_mtime_ns) == before
        assert connection.is_in_transaction()


@pytest.mark.parametrize(
    "fault",
    [
        "newline",
        "digest",
        "extra",
        "marker",
        "proof",
        "release",
        "revision",
        "files",
        "observed",
        "expired",
        "schema",
        "mapping",
        "duplicate",
        "oversize",
        "volume",
    ],
)
async def test_restore_denies_interrupted_mismatched_or_unpinned_cut(ready_node, tmp_path, fault):
    from awf.hosted_node.checkpoint_restore import validate_restored_cut

    async with cut(ready_node, tmp_path) as (connection, marker, roots, path):
        manifest, raw, release, authority = await restored(connection, marker, roots)
        if fault == "newline":
            raw = raw.rstrip()
        elif fault == "digest":
            raw = raw.replace(b"synthetic-disk-id", b"wrong")
        elif fault == "extra":
            manifest["point_in_time"] = authority.binding.point_in_time.isoformat()
        elif fault == "marker":
            manifest["maintenance_generation"] += 1
        elif fault == "proof":
            manifest["files"]["files_sha256"] = "0" * 64
        elif fault == "release":
            release = {**release, "core_image": "sha256:" + "0" * 64}
        elif fault == "revision":
            release = manifest["release"] = {**release, "core_revision": "unversioned"}
        elif fault == "files":
            path.write_bytes(b"different output\n")  # Same length/index, different content.
        elif fault == "observed":
            manifest["marker_observed_at"] = (
                authority.binding.point_in_time + timedelta(seconds=1)
            ).isoformat()
        elif fault == "expired":
            authority = replace(authority, expires_at=datetime.now(UTC) - timedelta(seconds=1))
        elif fault == "schema":
            authority = replace(
                authority, binding=authority.binding.model_copy(update={"schema_sha256": "0" * 64})
            )
        elif fault == "mapping":
            authority = replace(
                authority,
                binding=authority.binding.model_copy(update={"storage_mapping_sha256": ""}),
            )
        elif fault == "duplicate":
            raw = raw.replace(b'"schema_version":1', b'"schema_version":2,"schema_version":1')
        elif fault == "oversize":
            raw = b" " * 65537
        else:
            roots = replace(roots, persistent_volumes=("unsupported-db",))
        if fault in {"extra", "marker", "proof", "revision", "observed"}:
            raw = encoded(manifest)
        # Even authority-pinned malformed bytes cannot bypass the local validator.
        if fault != "digest":
            authority = replace(
                authority,
                binding=authority.binding.model_copy(
                    update={"manifest_sha256": hashlib.sha256(raw).hexdigest()}
                ),
            )
        reason = {
            "digest": "RESTORE_MANIFEST_MISMATCH",
            "marker": "RESTORE_MARKER_MISMATCH",
            "proof": "RESTORE_FILES_MISMATCH",
            "release": "RESTORE_RELEASE_MISMATCH",
            "revision": "RESTORE_RELEASE_MISMATCH",
            "files": "RESTORE_FILES_MISMATCH",
            "observed": "RESTORE_CUT_TIME_MISMATCH",
            "expired": "RESTORE_AUTHORITY_EXPIRED",
            "schema": "RESTORE_RELEASE_MISMATCH",
            "volume": "CHECKPOINT_UNSUPPORTED_VOLUME",
        }.get(fault, "RESTORE_MANIFEST_INVALID")
        with pytest.raises(MaterialDeniedError, match=f"^{reason}$"):
            await validate_restored_cut(
                connection, raw, authority, roots, release, time.monotonic() + 5
            )
