"""Read-only orphan resource detector tests."""

from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

import pytest

from awf.db.enums import WorkspaceStatus
from awf.service.gc import DEFAULT_MIN_AGE_HOURS
from awf.service.orphans import (
    WorkspaceIdView,
    WorkspaceLifecycleSnapshot,
    detect_orphan_resources,
)


class _Completed:
    def __init__(self, *, returncode: int = 0, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _run_with(
    *,
    containers: list[dict[str, object]] | str | Exception | None = None,
    networks: list[dict[str, object]] | str | Exception | None = None,
    volumes: list[dict[str, object]] | str | Exception | None = None,
) -> Any:
    payloads = {
        "ps": containers if containers is not None else [],
        "network": networks if networks is not None else [],
        "volume": volumes if volumes is not None else [],
    }

    def _run(
        args: list[str],
        *,
        check: bool,
        capture_output: bool,
        text: Literal[True],
        timeout: float,
        env: dict[str, str],
    ) -> _Completed:
        del check, capture_output, text, timeout, env
        if args[:3] == ["docker", "ps", "-a"]:
            return _result(payloads["ps"])
        if args[:3] == ["docker", "network", "ls"]:
            return _result(payloads["network"])
        if args[:3] == ["docker", "volume", "ls"]:
            return _result(payloads["volume"])
        raise AssertionError(f"unexpected docker command: {args}")

    return _run


def _result(payload: list[dict[str, object]] | str | Exception) -> _Completed:
    if isinstance(payload, Exception):
        raise payload
    if isinstance(payload, str):
        return _Completed(stdout=payload)
    return _Completed(stdout="".join(json.dumps(row) + "\n" for row in payload))


def _view(
    *snapshots: WorkspaceLifecycleSnapshot,
    available: bool = True,
) -> WorkspaceIdView:
    active_ids = frozenset(
        snapshot.workspace_id
        for snapshot in snapshots
        if snapshot.status
        in {
            WorkspaceStatus.requested.value,
            WorkspaceStatus.provisioning.value,
            WorkspaceStatus.ready.value,
            WorkspaceStatus.running.value,
            WorkspaceStatus.validating.value,
            WorkspaceStatus.pushing.value,
            WorkspaceStatus.monitoring_pr.value,
            WorkspaceStatus.destroying.value,
        }
    )
    terminal_ids = frozenset(
        snapshot.workspace_id
        for snapshot in snapshots
        if snapshot.status
        in {
            WorkspaceStatus.completed.value,
            WorkspaceStatus.failed.value,
            WorkspaceStatus.cancelled.value,
            WorkspaceStatus.destroyed.value,
        }
    )
    return WorkspaceIdView(
        active_ids=active_ids,
        terminal_ids=terminal_ids,
        available=available,
        snapshots=tuple(snapshots),
    )


def _snapshot(
    workspace_id: str,
    *,
    status: WorkspaceStatus = WorkspaceStatus.running,
    updated_at: datetime | None = None,
    compose_project_name: str | None = None,
) -> WorkspaceLifecycleSnapshot:
    return WorkspaceLifecycleSnapshot(
        workspace_id=workspace_id,
        status=status.value,
        updated_at=updated_at or datetime(2026, 4, 28, tzinfo=UTC),
        compose_project_name=compose_project_name or f"awf_{workspace_id}",
    )


def test_no_orphans_reports_zero_counts(tmp_path: Path) -> None:
    (tmp_path / "git" / "worktrees" / "ws_alive").mkdir(parents=True)
    summary = detect_orphan_resources(
        work_dir=tmp_path,
        docker_host="unix:///var/run/docker.sock",
        workspace_view=_view(_snapshot("ws_alive")),
        run_subprocess=_run_with(
            containers=[
                {
                    "id": "abc",
                    "name": "awf_ws_alive-agent-1",
                    "state": "running",
                    "status": "Up",
                    "project": "awf_ws_alive",
                    "service": "agent",
                },
                {
                    "id": "other",
                    "name": "not-awf-db-1",
                    "project": "not-awf",
                    "service": "db",
                },
            ],
            networks=[
                {
                    "id": "net",
                    "name": "awf_ws_alive_default",
                    "project": "awf_ws_alive",
                }
            ],
            volumes=[
                {
                    "name": "awf_ws_alive_postgres_data",
                    "project": "awf_ws_alive",
                }
            ],
        ),
        now=datetime(2026, 4, 28, tzinfo=UTC),
    ).to_check_payload()

    assert summary["ok"] is True
    assert summary["reason"] == "NO_ORPHANS"
    assert summary["orphan_count"] == 0
    assert summary["orphan_counts_by_kind"] == {}
    assert summary["resource_counts"] == {
        "container": 1,
        "network": 1,
        "volume": 1,
        "worktree": 1,
    }
    assert summary["examples"] == []


def test_orphan_container_missing_workspace_is_structured(tmp_path: Path) -> None:
    summary = detect_orphan_resources(
        work_dir=tmp_path,
        docker_host="unix:///var/run/docker.sock",
        workspace_view=_view(),
        run_subprocess=_run_with(
            containers=[
                {
                    "id": "abc",
                    "name": "awf_ws_ghost-agent-1",
                    "state": "exited",
                    "status": "Exited",
                    "project": "awf_ws_ghost",
                    "service": "agent",
                }
            ]
        ),
    ).to_check_payload()

    assert summary["ok"] is False
    assert summary["orphan_count"] == 1
    assert summary["orphan_counts_by_kind"] == {"container": 1}
    example = summary["examples"][0]
    assert example["resource_kind"] == "container"
    assert example["workspace_id"] == "ws_ghost"
    assert example["reason"] == "WORKSPACE_MISSING"
    assert example["classification"] == "cleanup_ready"
    assert example["suggested_action"]


def test_orphan_network_missing_workspace_is_structured(tmp_path: Path) -> None:
    summary = detect_orphan_resources(
        work_dir=tmp_path,
        docker_host="unix:///var/run/docker.sock",
        workspace_view=_view(),
        run_subprocess=_run_with(
            networks=[
                {
                    "id": "net1",
                    "name": "awf_ws_ghost_default",
                    "project": "awf_ws_ghost",
                }
            ]
        ),
    ).to_check_payload()

    assert summary["orphan_counts_by_kind"] == {"network": 1}
    example = summary["examples"][0]
    assert example["resource_kind"] == "network"
    assert example["workspace_id"] == "ws_ghost"
    assert example["reason"] == "WORKSPACE_MISSING"
    assert example["suggested_action"]


def test_orphan_volume_missing_workspace_is_structured_with_name_fallback(
    tmp_path: Path,
) -> None:
    summary = detect_orphan_resources(
        work_dir=tmp_path,
        docker_host="unix:///var/run/docker.sock",
        workspace_view=_view(),
        run_subprocess=_run_with(
            volumes=[
                {
                    "name": "awf_ws_ghost_postgres_data",
                    "project": "",
                }
            ]
        ),
    ).to_check_payload()

    assert summary["orphan_counts_by_kind"] == {"volume": 1}
    example = summary["examples"][0]
    assert example["resource_kind"] == "volume"
    assert example["workspace_id"] == "ws_ghost"
    assert example["reason"] == "WORKSPACE_MISSING"
    assert example["resource_name"] == "awf_ws_ghost_postgres_data"


def test_orphan_worktree_missing_workspace_is_structured(tmp_path: Path) -> None:
    (tmp_path / "git" / "worktrees" / "ws_ghost").mkdir(parents=True)
    summary = detect_orphan_resources(
        work_dir=tmp_path,
        docker_host="unix:///var/run/docker.sock",
        workspace_view=_view(),
        run_subprocess=_run_with(),
    ).to_check_payload()

    assert summary["orphan_counts_by_kind"] == {"worktree": 1}
    example = summary["examples"][0]
    assert example["resource_kind"] == "worktree"
    assert example["workspace_id"] == "ws_ghost"
    assert example["reason"] == "WORKSPACE_MISSING"
    assert example["suggested_action"]


def test_docker_unavailable_is_actionable_warning_not_crash(tmp_path: Path) -> None:
    (tmp_path / "git" / "worktrees" / "ws_ghost").mkdir(parents=True)

    summary = detect_orphan_resources(
        work_dir=tmp_path,
        docker_host="unix:///var/run/docker.sock",
        workspace_view=_view(),
        run_subprocess=_run_with(containers=FileNotFoundError("docker")),
    ).to_check_payload()

    assert summary["ok"] is False
    assert summary["reason"] == "ORPHANS_PRESENT"
    assert summary["orphan_counts_by_kind"] == {"worktree": 1}
    assert summary["warning_count"] == 1
    warning = summary["warnings"][0]
    assert warning["reason"] == "DOCKER_CLI_NOT_FOUND"
    assert warning["classification"] == "warning"
    assert warning["suggested_action"]


def test_malformed_docker_output_records_warning_and_keeps_valid_rows(
    tmp_path: Path,
) -> None:
    output = "\n".join(
        [
            "{not-json",
            json.dumps(
                {
                    "id": "abc",
                    "name": "awf_ws_ghost-agent-1",
                    "state": "exited",
                    "status": "Exited",
                    "project": "awf_ws_ghost",
                    "service": "agent",
                }
            ),
        ]
    )

    summary = detect_orphan_resources(
        work_dir=tmp_path,
        docker_host="unix:///var/run/docker.sock",
        workspace_view=_view(),
        run_subprocess=_run_with(containers=output),
    ).to_check_payload()

    assert summary["orphan_counts_by_kind"] == {"container": 1}
    assert summary["warning_count"] == 1
    assert summary["warnings"][0]["reason"] == "DOCKER_OUTPUT_MALFORMED"


def test_malformed_non_object_output_without_orphans_reports_warn(tmp_path: Path) -> None:
    summary = detect_orphan_resources(
        work_dir=tmp_path,
        docker_host="unix:///var/run/docker.sock",
        workspace_view=_view(),
        run_subprocess=_run_with(containers="\n[]\n"),
    ).to_check_payload()

    assert summary["ok"] is True
    assert summary["status"] == "warn"
    assert summary["reason"] == "DOCKER_OUTPUT_MALFORMED"
    assert summary["warning_count"] == 1
    assert "expected object" in str(summary["warnings"][0]["detail"])


def test_docker_timeout_and_generic_errors_are_actionable_warnings(tmp_path: Path) -> None:
    timeout = detect_orphan_resources(
        work_dir=tmp_path,
        docker_host="unix:///var/run/docker.sock",
        workspace_view=_view(),
        run_subprocess=_run_with(
            containers=subprocess.TimeoutExpired(["docker", "ps"], timeout=5)
        ),
    ).to_check_payload()
    generic = detect_orphan_resources(
        work_dir=tmp_path,
        docker_host="unix:///var/run/docker.sock",
        workspace_view=_view(),
        run_subprocess=_run_with(containers=RuntimeError("daemon exploded")),
    ).to_check_payload()

    assert timeout["ok"] is True
    assert timeout["status"] == "unavailable"
    assert timeout["reason"] == "DOCKER_UNAVAILABLE"
    assert "timed out" in str(timeout["detail"])
    assert generic["reason"] == "DOCKER_UNAVAILABLE"
    assert "RuntimeError: daemon exploded" in str(generic["detail"])


def test_label_parsing_and_custom_compose_projects_are_expected(tmp_path: Path) -> None:
    now = datetime(2026, 4, 28, tzinfo=UTC)
    summary = detect_orphan_resources(
        work_dir=tmp_path,
        docker_host="unix:///var/run/docker.sock",
        workspace_view=_view(
            _snapshot(
                "ws_custom",
                updated_at=now,
                compose_project_name="custom_project",
            ),
            WorkspaceLifecycleSnapshot(
                workspace_id="ws_default",
                status=WorkspaceStatus.running.value,
                updated_at=now,
                compose_project_name=None,
            ),
        ),
        run_subprocess=_run_with(
            containers=[
                {
                    "ID": "abc",
                    "Name": "custom-agent-1",
                    "Labels": {"com.docker.compose.project": "custom_project"},
                },
                {
                    "ID": "def",
                    "Name": "awf_ws_default-agent-1",
                    "Project": "awf_ws_default",
                },
            ],
            networks=[
                {
                    "ID": "net",
                    "Name": "custom_default",
                    "Labels": "com.docker.compose.project=custom_project,other=x",
                }
            ],
        ),
        now=now,
    ).to_check_payload()

    assert summary["reason"] == "NO_ORPHANS"
    assert summary["active_count"] == 2
    assert summary["expected_count"] == 3
    assert summary["resource_counts"] == {"container": 2, "network": 1}


def test_volume_name_fallback_handles_dash_prefix_and_skips_unmanaged_names(
    tmp_path: Path,
) -> None:
    summary = detect_orphan_resources(
        work_dir=tmp_path,
        docker_host="unix:///var/run/docker.sock",
        workspace_view=_view(),
        run_subprocess=_run_with(
            volumes=[
                {"name": "awf-ws_dash_postgres_data", "project": ""},
                {"name": "not_awf_postgres_data", "project": ""},
                {"name": "awf_ws_bad_postgres_data", "project": "not-awf"},
            ]
        ),
    ).to_check_payload()

    assert summary["orphan_counts_by_kind"] == {"volume": 2}
    examples = summary["examples"]
    assert [example["workspace_id"] for example in examples] == ["ws_dash", "ws_bad"]
    assert examples[0]["compose_project"] == "awf-ws_dash"
    assert examples[1]["compose_project"] == "not-awf"


def test_worktree_scan_skips_unmanaged_entries_and_reports_scan_warning(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "git" / "worktrees"
    root.mkdir(parents=True)
    (root / "not_ws").mkdir()
    (root / "ws_file").write_text("not a directory", encoding="utf-8")

    summary = detect_orphan_resources(
        work_dir=tmp_path,
        docker_host="unix:///var/run/docker.sock",
        workspace_view=_view(),
        run_subprocess=_run_with(),
    ).to_check_payload()

    assert summary["resource_counts"] == {}
    assert summary["examples"] == []

    original_iterdir = Path.iterdir

    def _raise_for_worktrees(path: Path) -> Any:
        if path == root:
            raise OSError("permission denied")
        return original_iterdir(path)

    monkeypatch.setattr(Path, "iterdir", _raise_for_worktrees)

    warning_summary = detect_orphan_resources(
        work_dir=tmp_path,
        docker_host="unix:///var/run/docker.sock",
        workspace_view=_view(),
        run_subprocess=_run_with(),
    ).to_check_payload()

    assert warning_summary["status"] == "warn"
    assert warning_summary["reason"] == "WORKTREE_SCAN_FAILED"
    assert warning_summary["warnings"][0]["suggested_action"].startswith("Fix permissions")


def test_unknown_workspace_status_and_naive_retention_timestamp(tmp_path: Path) -> None:
    now = datetime(2026, 4, 28, tzinfo=UTC)
    unknown = detect_orphan_resources(
        work_dir=tmp_path,
        docker_host="unix:///var/run/docker.sock",
        workspace_view=_view(
            WorkspaceLifecycleSnapshot(
                workspace_id="ws_future",
                status="future_status",
                updated_at=now,
                compose_project_name="awf_ws_future",
            )
        ),
        run_subprocess=_run_with(
            containers=[
                {
                    "id": "abc",
                    "name": "awf_ws_future-agent-1",
                    "project": "awf_ws_future",
                }
            ]
        ),
        now=now,
    ).to_check_payload()

    retained = detect_orphan_resources(
        work_dir=tmp_path,
        docker_host="unix:///var/run/docker.sock",
        workspace_view=_view(
            _snapshot(
                "ws_naive",
                status=WorkspaceStatus.completed,
                updated_at=(now - timedelta(hours=1)).replace(tzinfo=None),
            )
        ),
        run_subprocess=_run_with(
            containers=[
                {
                    "id": "def",
                    "name": "awf_ws_naive-agent-1",
                    "project": "awf_ws_naive",
                }
            ]
        ),
        now=now,
    ).to_check_payload()

    assert unknown["examples"][0]["reason"] == "WORKSPACE_STATUS_UNKNOWN"
    assert retained["reason"] == "NO_ORPHANS"
    assert retained["retained_count"] == 1


def test_completed_workspace_within_retention_not_flagged(tmp_path: Path) -> None:
    now = datetime(2026, 4, 28, tzinfo=UTC)
    (tmp_path / "git" / "worktrees" / "ws_done").mkdir(parents=True)

    summary = detect_orphan_resources(
        work_dir=tmp_path,
        docker_host="unix:///var/run/docker.sock",
        workspace_view=_view(
            _snapshot(
                "ws_done",
                status=WorkspaceStatus.completed,
                updated_at=now - timedelta(hours=DEFAULT_MIN_AGE_HOURS - 1),
            )
        ),
        run_subprocess=_run_with(
            containers=[
                {
                    "id": "abc",
                    "name": "awf_ws_done-agent-1",
                    "state": "exited",
                    "status": "Exited",
                    "project": "awf_ws_done",
                    "service": "agent",
                }
            ],
            networks=[
                {
                    "id": "net",
                    "name": "awf_ws_done_default",
                    "project": "awf_ws_done",
                }
            ],
            volumes=[
                {
                    "name": "awf_ws_done_postgres_data",
                    "project": "awf_ws_done",
                }
            ],
        ),
        now=now,
    ).to_check_payload()

    assert summary["ok"] is True
    assert summary["reason"] == "NO_ORPHANS"
    assert summary["retained_count"] == 4
    assert summary["retained_counts_by_kind"] == {
        "container": 1,
        "network": 1,
        "volume": 1,
        "worktree": 1,
    }
    assert summary["orphan_count"] == 0
    assert summary["examples"] == []


def test_completed_workspace_past_retention_is_cleanup_ready(tmp_path: Path) -> None:
    now = datetime(2026, 4, 28, tzinfo=UTC)
    summary = detect_orphan_resources(
        work_dir=tmp_path,
        docker_host="unix:///var/run/docker.sock",
        workspace_view=_view(
            _snapshot(
                "ws_old",
                status=WorkspaceStatus.completed,
                updated_at=now - timedelta(hours=DEFAULT_MIN_AGE_HOURS + 1),
            )
        ),
        run_subprocess=_run_with(
            containers=[
                {
                    "id": "abc",
                    "name": "awf_ws_old-agent-1",
                    "state": "exited",
                    "status": "Exited",
                    "project": "awf_ws_old",
                    "service": "agent",
                }
            ]
        ),
        now=now,
    ).to_check_payload()

    assert summary["ok"] is False
    assert summary["orphan_count"] == 1
    example = summary["examples"][0]
    assert example["resource_kind"] == "container"
    assert example["workspace_id"] == "ws_old"
    assert example["reason"] == "WORKSPACE_TERMINAL_RETENTION_EXPIRED"
    assert example["classification"] == "cleanup_ready"
