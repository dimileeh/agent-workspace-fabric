"""Read-only, bounded Core durable-prefix proof on an already held SQL cut.

The host owns producer exclusion, filesystem sealing and provider capture. This
helper neither acquires/relinquishes that exclusion nor certifies companion DBs.
"""

import asyncio
import hashlib
import json
import os
import re
import stat
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import yaml

from awf.hosted_node.checkpoint_schemas import CHECKPOINT_SCHEMA_REVISION, CheckpointMarker
from awf.hosted_node.identity import MaterialDeniedError

TABLES = tuple(
    sorted(
        [
            "alembic_version",
            "workspaces",
            "tasks",
            "task_attempts",
            "operations",
            "validation_runs",
            "resource_reservations",
            "service_gc_requests",
            "callback_deliveries",
            "callback_subscriptions",
            "merge_candidates",
            "workspace_events",
            "workspace_secret_leases",
            "provider_model_circuit_breakers",
            "queue_decisions",
            "pr_feedback_resolutions",
            "stale_reasons",
            "policy_findings",
            "workspace_log_streams",
            "egress_audit_records",
            "operator_grant_audit_records",
            "worker_heartbeats",
            "hosted_owners",
            "hosted_preparations",
            "hosted_grants",
            "hosted_receipts",
            "hosted_node_lifecycle",
            "hosted_restore_reconciliations",
            "hosted_restore_row_holds",
        ]
    )
)
VALIDATOR_VERSION = "core-durable-cut.v1"


class HeldConnection(Protocol):
    """The existing asyncpg barrier connection, not a connection pool."""

    def is_in_transaction(self) -> bool: ...
    async def fetch(self, query: str, *args: Any) -> Any: ...
    async def fetchval(self, query: str, *args: Any) -> Any: ...


@dataclass(frozen=True)
class CheckpointRoots:
    work_dir: Path
    device: int
    image_digest: str
    schema_sha256: str
    persistent_volumes: tuple[str, ...] = ()
    max_files: int = 100_000
    max_bytes: int = 1_073_741_824


@dataclass(frozen=True)
class DurableCutProof:
    validator_version: str
    image_digest: str
    marker_sha256: str
    schema_sha256: str
    files_sha256: str
    files: int
    byte_count: int
    log_streams: int
    volatile_exclusions: tuple[str, ...] = ("tmpfs_credentials", "unflushed_redaction_buffers")


def digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
    ).hexdigest()


def require(condition: bool, reason: str) -> None:
    if not condition:
        raise MaterialDeniedError("CHECKPOINT_" + reason)


def _publications(root: Path, metadata: dict[str, bytes]) -> None:
    for name, raw in metadata.items():
        path = Path(name)
        if path.parts[0] == "artifacts" and path.suffix == ".json":
            json.loads(raw)
        if path.parts[0] == "compose" and path.suffix in {".yml", ".yaml"}:
            document = yaml.safe_load(raw)
            require(
                isinstance(document, dict) and isinstance(document.get("services"), dict),
                "COMPOSE_INVALID",
            )
            require(not document.get("volumes"), "UNSUPPORTED_VOLUME")
            for service in document["services"].values():
                require(isinstance(service, dict), "COMPOSE_INVALID")
                for volume in service.get("volumes", []):
                    require(isinstance(volume, (str, dict)), "COMPOSE_INVALID")
                    source = (
                        volume.split(":", 1)[0]
                        if isinstance(volume, str)
                        else volume.get("source", "")
                    )
                    # Named volumes, host-external binds and Docker sockets are
                    # not silently certified as application-consistent storage.
                    candidate = Path(source)
                    require(
                        candidate.is_absolute()
                        and candidate.resolve().is_relative_to(root / "git"),
                        "UNSUPPORTED_VOLUME",
                    )
        if len(path.parts) == 4 and path.parts[:2] == ("git", "worktrees") and path.name == ".git":
            value = raw.decode().strip()
            require(value.startswith("gitdir: "), "GIT_LINK_INVALID")
            target = Path(value.removeprefix("gitdir: "))
            target = (root / path.parent / target).resolve()
            require(target.is_relative_to(root / "git" / "mirrors"), "GIT_LINK_INVALID")
            relative = target.relative_to(root)
            back = metadata.get(str(relative / "gitdir"), b"").decode().strip()
            common = metadata.get(str(relative / "commondir"), b"").decode().strip()
            head = metadata.get(str(relative / "HEAD"), b"").decode().strip()
            require(bool(back and common and head), "GIT_LINK_INVALID")
            require((target / back).resolve() == root / path, "GIT_LINK_INVALID")
            require((target / common).resolve() == target.parent.parent, "GIT_LINK_INVALID")
            require(
                re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64}|ref: refs/[A-Za-z0-9_./-]+)", head)
                is not None,
                "GIT_HEAD_INVALID",
            )


async def schema_fingerprint(connection: HeldConnection) -> str:
    """Definitions and ACLs, never row contents or credentials."""
    queries = {
        "relations": "SELECT relname, relkind::text, relowner::regrole::text, relacl::text, relrowsecurity, relforcerowsecurity FROM pg_class WHERE relnamespace=current_schema()::regnamespace ORDER BY relname",
        "columns": "SELECT table_name,column_name,ordinal_position,data_type,udt_name,is_nullable,column_default FROM information_schema.columns WHERE table_schema=current_schema() ORDER BY table_name,ordinal_position",
        "constraints": "SELECT c.relname,x.conname,pg_get_constraintdef(x.oid) AS definition FROM pg_constraint x JOIN pg_class c ON c.oid=x.conrelid WHERE c.relnamespace=current_schema()::regnamespace ORDER BY c.relname,x.conname",
        "triggers": "SELECT c.relname,t.tgname,t.tgenabled::text,pg_get_triggerdef(t.oid) AS definition FROM pg_trigger t JOIN pg_class c ON c.oid=t.tgrelid WHERE c.relnamespace=current_schema()::regnamespace AND NOT t.tgisinternal ORDER BY c.relname,t.tgname",
        "functions": "SELECT p.proname,p.proowner::regrole::text,p.proacl::text,p.prosecdef,pg_get_functiondef(p.oid) AS definition FROM pg_proc p WHERE p.pronamespace=current_schema()::regnamespace ORDER BY p.proname,p.oid::regprocedure::text",
        "indexes": "SELECT indexname,indexdef FROM pg_indexes WHERE schemaname=current_schema() ORDER BY indexname",
    }
    return digest(
        {
            key: [dict(row) for row in await connection.fetch(query)]
            for key, query in queries.items()
        }
    )


async def _barrier(
    connection: HeldConnection, marker: CheckpointMarker, roots: CheckpointRoots
) -> None:
    require(connection.is_in_transaction(), "BARRIER_REQUIRED")
    tables = await connection.fetch(
        "SELECT c.relname, EXISTS(SELECT 1 FROM pg_locks l WHERE l.relation=c.oid AND l.pid=pg_backend_pid() AND l.granted AND l.mode IN ('ExclusiveLock','AccessExclusiveLock')) AS held FROM pg_class c WHERE c.relnamespace=current_schema()::regnamespace AND c.relkind IN ('r','p') ORDER BY c.relname"
    )
    require(
        tuple(row["relname"] for row in tables) == TABLES and all(row["held"] for row in tables),
        "BARRIER_REQUIRED",
    )
    versions = await connection.fetch("SELECT version_num FROM alembic_version")
    require(
        [row["version_num"] for row in versions] == [CHECKPOINT_SCHEMA_REVISION]
        and marker.schema_revision == CHECKPOINT_SCHEMA_REVISION,
        "SCHEMA_MISMATCH",
    )
    rows = await connection.fetch("SELECT to_jsonb(n) AS value FROM hosted_node_lifecycle n")
    require(len(rows) == 1, "MARKER_MISMATCH")
    value = rows[0]["value"]
    row = json.loads(value) if isinstance(value, str) else value
    require(
        row["checkpoint_marker"] == marker.model_dump(mode="json")
        and row["identity"] == marker.identity.model_dump()
        and row["maintenance_generation"] == marker.maintenance_generation
        and row["phase"] == "ready"
        and row["recovery_key"] is not None
        and row["drain_generation"] is None
        and row["disk_ref"] == marker.disk_ref
        and row["database_ref"] == marker.database_ref
        and row.get("restore_quarantine") is None,
        "MARKER_MISMATCH",
    )
    require(await schema_fingerprint(connection) == roots.schema_sha256, "SCHEMA_MISMATCH")


def _files(
    roots: CheckpointRoots, logs: list[Mapping[str, Any]], deadline: float
) -> tuple[str, int, int]:
    require(
        roots.work_dir.is_absolute() and roots.work_dir.resolve() == roots.work_dir, "UNSAFE_ROOT"
    )
    require(not roots.persistent_volumes, "UNSUPPORTED_VOLUME")
    require(
        0 < roots.max_files <= 100_000 and 0 < roots.max_bytes <= 1_073_741_824, "INVENTORY_LIMIT"
    )
    require(
        re.fullmatch(r"sha256:[0-9a-f]{64}", roots.image_digest) is not None, "IMAGE_PIN_REQUIRED"
    )
    expected = {str(Path(row["path"]).relative_to(roots.work_dir)): row for row in logs}
    require(len(expected) == len(logs), "DUPLICATE_LOG_PATH")
    seen: set[str] = set()
    count = byte_count = entries = 0
    evidence = hashlib.sha256()
    metadata: dict[str, bytes] = {}
    # Known top-level durable roots. service-auth and unknown mounts are not
    # silently omitted; credential snapshots require a separately reviewed cut.
    allowed = {"git", "logs", "artifacts", "compose", "hosted-empty-home"}
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    root_fd = os.open(roots.work_dir, flags)

    def visit(directory: int, parts: tuple[str, ...]) -> None:
        nonlocal count, byte_count, entries
        require(time.monotonic() < deadline, "EXPIRED")
        require(os.fstat(directory).st_dev == roots.device, "UNSAFE_DEVICE")
        with os.scandir(directory) as stream:
            names = []
            for entry in stream:
                entries += 1
                require(entries <= roots.max_files * 2, "INVENTORY_LIMIT")
                names.append(entry.name)
        for name in sorted(names):
            require(time.monotonic() < deadline, "EXPIRED")
            if not parts:
                require(name in allowed, "UNSUPPORTED_ROOT")
            path = (*parts, name)
            relative = "/".join(path)
            info = os.stat(name, dir_fd=directory, follow_symlinks=False)
            require(info.st_dev == roots.device, "UNSAFE_DEVICE")
            require(not stat.S_ISLNK(info.st_mode), "UNSUPPORTED_SYMLINK")
            if stat.S_ISDIR(info.st_mode):
                child = os.open(name, flags, dir_fd=directory)
                try:
                    visit(child, path)
                finally:
                    os.close(child)
                continue
            require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1, "UNSAFE_FILE")
            require(
                not name.endswith((".tmp", ".pending", ".lock")) and name != "alternates",
                "PUBLICATION_INCOMPLETE",
            )
            count += 1
            byte_count += info.st_size
            require(count <= roots.max_files and byte_count <= roots.max_bytes, "INVENTORY_LIMIT")
            content = hashlib.sha256()
            inspect = path[0] in {"artifacts", "compose"} or (
                path[0] == "git" and name in {".git", "gitdir", "commondir", "HEAD"}
            )
            document = bytearray()
            length = lines = 0
            fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=directory)
            try:
                actual = os.fstat(fd)
                require(
                    (actual.st_dev, actual.st_ino, actual.st_size, actual.st_mtime_ns)
                    == (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns),
                    "FILE_CHANGED",
                )
                while chunk := os.read(fd, min(65536, roots.max_bytes + 1)):
                    require(time.monotonic() < deadline, "EXPIRED")
                    length += len(chunk)
                    require(length <= info.st_size, "FILE_CHANGED")
                    content.update(chunk)
                    if inspect:
                        require(length <= 1_048_576, "PUBLICATION_LIMIT")
                        document.extend(chunk)
                    lines += chunk.count(b"\n")
                require(length == info.st_size, "FILE_CHANGED")
                require(os.fstat(fd).st_mtime_ns == info.st_mtime_ns, "FILE_CHANGED")
            finally:
                os.close(fd)
            if path[0] == "logs":
                require(relative in expected, "UNINDEXED_LOG")
                log = expected[relative]
                require(
                    length == log["byte_count"] and lines == log["line_count"],
                    "LOG_PREFIX_MISMATCH",
                )
                seen.add(relative)
            if inspect:
                metadata[relative] = bytes(document)
            evidence.update(
                json.dumps(
                    [relative, info.st_mode, length, content.hexdigest()], separators=(",", ":")
                ).encode()
                + b"\n"
            )

    try:
        visit(root_fd, ())
    finally:
        os.close(root_fd)
    require(seen == set(expected), "LOG_MISSING")
    _publications(roots.work_dir, metadata)
    return evidence.hexdigest(), count, byte_count


async def validate_checkpoint_cut(
    connection: HeldConnection,
    expected_marker: CheckpointMarker,
    pinned_roots: CheckpointRoots,
    monotonic_deadline: float,
) -> DurableCutProof:
    """Validate, never repair; callers seal only after this exact held cut passes."""
    require(0 < monotonic_deadline - time.monotonic() <= 60, "EXPIRED")
    try:
        async with asyncio.timeout(monotonic_deadline - time.monotonic()):
            return await _validate_cut(
                connection, expected_marker, pinned_roots, monotonic_deadline
            )
    except TimeoutError:
        raise MaterialDeniedError("CHECKPOINT_EXPIRED") from None


async def _validate_cut(
    connection: HeldConnection,
    expected_marker: CheckpointMarker,
    pinned_roots: CheckpointRoots,
    monotonic_deadline: float,
) -> DurableCutProof:
    try:
        await _barrier(connection, expected_marker, pinned_roots)
        logs = await connection.fetch(
            "SELECT path,byte_count,line_count FROM workspace_log_streams ORDER BY workspace_id,stream_id LIMIT $1",
            pinned_roots.max_files + 1,
        )
        require(len(logs) <= pinned_roots.max_files, "INVENTORY_LIMIT")
        task = asyncio.create_task(
            asyncio.to_thread(_files, pinned_roots, logs, monotonic_deadline)
        )
        try:
            while not task.done():
                await asyncio.wait({task}, timeout=0.25)
                if not task.done():
                    # Keep the same barrier lease alive while bounded local reads
                    # run, without a detached thread continuing after return.
                    await connection.fetchval("SELECT 1")
            files_sha256, count, byte_count = task.result()
        finally:
            while not task.done():
                try:
                    await asyncio.shield(task)
                except asyncio.CancelledError:
                    continue
        await _barrier(connection, expected_marker, pinned_roots)
        require(time.monotonic() < monotonic_deadline, "EXPIRED")
        return DurableCutProof(
            VALIDATOR_VERSION,
            pinned_roots.image_digest,
            digest(expected_marker.model_dump(mode="json")),
            pinned_roots.schema_sha256,
            files_sha256,
            count,
            byte_count,
            len(logs),
        )
    except (OSError, ValueError, KeyError, TypeError, yaml.YAMLError) as exc:
        if isinstance(exc, MaterialDeniedError):
            raise
        raise MaterialDeniedError("CHECKPOINT_FILE_EVIDENCE_UNAVAILABLE") from None
