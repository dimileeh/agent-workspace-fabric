"""Branch-edge coverage for ``_load_terminal_runtime_candidate`` guard branches.

Split out of ``test_cleanup_branch_edges.py`` to keep each test file under the
maintainability line limit. These monkeypatch ``run_db_operation_with_retry`` to
return a single fetched row (mirroring
``test_list_terminal_runtime_candidates_skips_rows_without_repo_url``), exercising
every guard branch without a real DB. The DB-backed happy paths are covered by
``test_worker_parts/test_worker_part_051.py``.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from awf.control.worker import cleanup as worker_cleanup
from awf.db.enums import WorkspaceStatus

# --- _load_terminal_runtime_candidate guard branches (#583, #584) ---


def _load_candidate_worker(row: Any, *, node_id: str | None) -> SimpleNamespace:
    async def _run_db_operation_with_retry(*_args: Any, **_kwargs: Any) -> Any:
        return row

    return SimpleNamespace(
        _config=SimpleNamespace(node_id=node_id),
        _session_factory=lambda: object(),
        _log_transient_db_retry=lambda *_a: None,
        _run=_run_db_operation_with_retry,
    )


@pytest.mark.unit
async def test_load_terminal_runtime_candidate_returns_none_when_row_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unknown workspace id (no row) yields no candidate."""
    worker = _load_candidate_worker(None, node_id="node-a")
    monkeypatch.setattr(worker_cleanup, "run_db_operation_with_retry", worker._run)

    result = await worker_cleanup._load_terminal_runtime_candidate(worker, "ws_missing")  # noqa: SLF001

    assert result is None


@pytest.mark.unit
async def test_load_terminal_runtime_candidate_returns_none_when_not_terminal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-terminal status is skipped — the prompt hook is a no-op for it."""
    row = (
        WorkspaceStatus.running.value,
        "https://example.test/r.git",
        "awf_ws",
        "/tmp/ws/compose.yml",
        None,
    )
    worker = _load_candidate_worker(row, node_id="node-a")
    monkeypatch.setattr(worker_cleanup, "run_db_operation_with_retry", worker._run)

    result = await worker_cleanup._load_terminal_runtime_candidate(worker, "ws_running")  # noqa: SLF001

    assert result is None


@pytest.mark.unit
async def test_load_terminal_runtime_candidate_returns_none_when_repo_url_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A terminal row with an empty repo_url is not a candidate."""
    row = (WorkspaceStatus.failed.value, "", "awf_ws", None, None)
    worker = _load_candidate_worker(row, node_id="node-a")
    monkeypatch.setattr(worker_cleanup, "run_db_operation_with_retry", worker._run)

    result = await worker_cleanup._load_terminal_runtime_candidate(worker, "ws_no_repo")  # noqa: SLF001

    assert result is None


@pytest.mark.unit
async def test_load_terminal_runtime_candidate_returns_none_for_foreign_node(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A terminal row owned by another node is skipped (claim model respected)."""
    row = (
        WorkspaceStatus.completed.value,
        "https://example.test/r.git",
        "awf_ws",
        None,
        "some-other-node",
    )
    worker = _load_candidate_worker(row, node_id="node-a")
    monkeypatch.setattr(worker_cleanup, "run_db_operation_with_retry", worker._run)

    result = await worker_cleanup._load_terminal_runtime_candidate(worker, "ws_foreign")  # noqa: SLF001

    assert result is None


@pytest.mark.unit
async def test_load_terminal_runtime_candidate_builds_candidate_for_local_node(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A terminal row owned by this node is turned into a release candidate."""
    row = (
        WorkspaceStatus.cancelled.value,
        "https://example.test/r.git",
        "awf_ws",
        "/tmp/ws/compose.yml",
        "node-a",
    )
    worker = _load_candidate_worker(row, node_id="node-a")
    monkeypatch.setattr(worker_cleanup, "run_db_operation_with_retry", worker._run)

    result = await worker_cleanup._load_terminal_runtime_candidate(worker, "ws_local")  # noqa: SLF001

    assert result is not None
    assert result.workspace_id == "ws_local"
    assert result.status is WorkspaceStatus.cancelled
    assert result.repo_url == "https://example.test/r.git"
    assert result.compose_project_name == "awf_ws"
    assert result.compose_file_path == "/tmp/ws/compose.yml"


@pytest.mark.unit
async def test_load_terminal_runtime_candidate_builds_candidate_for_null_node(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A terminal row with a NULL node_id is owned locally (single-node fallback,
    mirroring ``_list_terminal_runtime_candidates``)."""
    row = (
        WorkspaceStatus.completed.value,
        "https://example.test/r.git",
        None,
        None,
        None,
    )
    worker = _load_candidate_worker(row, node_id=None)
    monkeypatch.setattr(worker_cleanup, "run_db_operation_with_retry", worker._run)

    result = await worker_cleanup._load_terminal_runtime_candidate(worker, "ws_null_node")  # noqa: SLF001

    assert result is not None
    assert result.workspace_id == "ws_null_node"
    assert result.compose_project_name is None
    assert result.compose_file_path is None
