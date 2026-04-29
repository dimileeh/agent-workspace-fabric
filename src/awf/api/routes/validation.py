"""Validation provenance endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from awf.api.deps import get_db_session
from awf.api.schemas import ValidationProvenanceListResponse
from awf.service.validation_provenance import (
    _base_stream_id,
    _build_persisted_validation_items,
    _build_validation_items,
    _closed_at,
    _command_index,
    _command_lookup,
    _command_phase,
    _command_record,
    _command_stream_ids,
    _command_text,
    _CommandRecord,
    _current_target_head_sha,
    _ensure_utc,
    _failed_record,
    _group_streams,
    _label,
    _normalize_phase,
    _opened_at,
    _phase_and_index,
    _phase_from_stream_name,
    _record_status,
    _resolved_profile,
    _run_commands,
    _stream_fd,
    _StreamPair,
    list_validation_provenance_response,
)

router = APIRouter(prefix="/v1/workspaces/{workspace_id}/validation", tags=["validation"])

__all__ = [
    "_CommandRecord",
    "_StreamPair",
    "_base_stream_id",
    "_build_persisted_validation_items",
    "_build_validation_items",
    "_closed_at",
    "_command_index",
    "_command_lookup",
    "_command_phase",
    "_command_record",
    "_command_stream_ids",
    "_command_text",
    "_current_target_head_sha",
    "_ensure_utc",
    "_failed_record",
    "_group_streams",
    "_label",
    "_normalize_phase",
    "_opened_at",
    "_phase_and_index",
    "_phase_from_stream_name",
    "_record_status",
    "_resolved_profile",
    "_run_commands",
    "_stream_fd",
    "list_validation_provenance",
]


@router.get("", response_model=ValidationProvenanceListResponse)
async def list_validation_provenance(
    workspace_id: str,
    session: AsyncSession = Depends(get_db_session),
) -> ValidationProvenanceListResponse:
    response = await list_validation_provenance_response(
        session,
        workspace_id=workspace_id,
    )
    if response is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error_code": "NOT_FOUND", "message": f"No workspace with id {workspace_id}"},
        )
    return response
