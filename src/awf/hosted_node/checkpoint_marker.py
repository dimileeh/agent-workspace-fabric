"""Commit intent before the external freeze/barrier; this is not capture completion."""

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from awf.hosted_node.checkpoint_schemas import (
    CHECKPOINT_SCHEMA_REVISION,
    CheckpointMarker,
    CheckpointRequest,
    marker_payload,
)
from awf.hosted_node.identity import MaterialDeniedError
from awf.hosted_node.lifecycle_models import NodeLifecycleRow
from awf.hosted_node.schemas import Identity


async def prepare_marker(
    session: AsyncSession,
    row: NodeLifecycleRow,
    generation: int,
    request: CheckpointRequest | None,
) -> None:
    previous = (
        CheckpointMarker.model_validate(row.checkpoint_marker) if row.checkpoint_marker else None
    )
    if (
        request is not None
        and previous
        and request.checkpoint_id == previous.checkpoint_id
        and (
            request.request_sha256 != previous.request_sha256
            or generation != previous.maintenance_generation
            or row.identity != previous.identity.model_dump()
        )
    ):
        raise MaterialDeniedError("CHECKPOINT_CONFLICT")
    if (
        previous
        and previous.maintenance_generation > row.maintenance_released_through
        and row.maintenance_generation is not None
    ):
        if generation != previous.maintenance_generation or (
            request is not None
            and (
                request.checkpoint_id != previous.checkpoint_id
                or request.request_sha256 != previous.request_sha256
                or row.identity != previous.identity.model_dump()
            )
        ):
            raise MaterialDeniedError("CHECKPOINT_CONFLICT")
        if request is not None:
            return
    if request is None:
        return
    if row.restore_quarantine is not None:
        raise MaterialDeniedError("RESTORE_QUARANTINED")
    if row.phase != "ready" or row.recovery_key is None or row.drain_generation is not None:
        raise MaterialDeniedError("CHECKPOINT_REQUIRES_ACCEPTED_NODE")
    revisions = (
        (await session.execute(text("SELECT version_num FROM alembic_version"))).scalars().all()
    )
    if revisions != [CHECKPOINT_SCHEMA_REVISION]:
        raise MaterialDeniedError("CHECKPOINT_SCHEMA_MISMATCH")
    await session.execute(text("SET LOCAL synchronous_commit = on"))
    now = await session.scalar(text("SELECT clock_timestamp()"))
    row.checkpoint_marker = marker_payload(
        request,
        Identity.model_validate(row.identity),
        generation,
        row.disk_ref,
        row.database_ref,
        now,
    ).model_dump(mode="json")
