import pytest
from unittest.mock import patch
from datetime import datetime, timedelta, UTC
from pathlib import Path
from awf.db.models import Workspace
from awf.db.enums import WorkspaceStatus
from awf.service.gc import _classify_workspace_for_gc, WorkspaceGCPreserved

def test_classify_workspace_failed_has_work_but_expired_no_default_policy():
    ws = Workspace(
        id="ws_1",
        status=WorkspaceStatus.failed.value,
        updated_at=datetime.now(UTC) - timedelta(hours=25),
        compose_project_name="proj"
    )
    with patch("awf.service.gc._failed_terminal_workspace_has_no_work", return_value=False):
        res = _classify_workspace_for_gc(ws, work_dir=Path("/tmp"), now=datetime.now(UTC), cutoff_at=datetime.now(UTC) - timedelta(hours=24), default_policy=False, cleanup_enabled=True)
        assert isinstance(res, WorkspaceGCPreserved)
