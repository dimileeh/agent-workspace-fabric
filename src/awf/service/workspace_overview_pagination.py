"""Cursor encoding for the workspace overview projection."""

from __future__ import annotations

import base64
import binascii
import json
from dataclasses import dataclass
from datetime import datetime

from awf.db.models import Workspace


@dataclass(frozen=True)
class _WorkspaceOverviewCursor:
    created_at: datetime
    workspace_id: str


class InvalidWorkspaceOverviewCursorError(ValueError):
    """Raised when a workspace overview pagination cursor cannot be decoded."""


def _encode_overview_cursor(workspace: Workspace) -> str:
    payload = {
        "t": workspace.created_at.isoformat(),
        "id": workspace.id,
    }
    encoded = base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    return encoded.decode("ascii").rstrip("=")


def _decode_overview_cursor(cursor: str | None) -> _WorkspaceOverviewCursor | None:
    if cursor is None:
        return None
    try:
        padded_cursor = cursor + ("=" * (-len(cursor) % 4))
        decoded = base64.urlsafe_b64decode(padded_cursor.encode("ascii"))
        payload = json.loads(decoded.decode("utf-8"))
        created_at = datetime.fromisoformat(
            payload["t"] if "t" in payload else payload["created_at"]
        )
        workspace_id = payload["id"] if "id" in payload else payload["workspace_id"]
    except (
        binascii.Error,
        KeyError,
        TypeError,
        UnicodeDecodeError,
        ValueError,
        json.JSONDecodeError,
    ) as exc:
        raise InvalidWorkspaceOverviewCursorError("Invalid workspace overview cursor") from exc
    if not isinstance(workspace_id, str) or workspace_id == "":
        raise InvalidWorkspaceOverviewCursorError("Invalid workspace overview cursor")
    return _WorkspaceOverviewCursor(created_at=created_at, workspace_id=workspace_id)
