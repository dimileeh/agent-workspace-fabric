"""Orphan AWF Docker resource and worktree detection."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.exc import SQLAlchemyError

from awf.service import orphan_resources
from awf.service.orphan_resources import (
    DetectedResource,
    ResourceScan,
    WorkspaceIdView,
    build_orphan_resource_summary,
    default_workspace_id_lookup,
    docker_resource_commands,
    empty_docker_scan,
    empty_worktree_scan,
    legacy_orphan_workspaces_payload,
    parse_docker_resource_rows,
    scan_docker_resources,
    scan_docker_resources_async,
    scan_managed_worktrees,
    summary_not_collected,
    unavailable_workspace_view,
    workspace_id_from_project,
)


class _Completed:
    def __init__(self, *, returncode: int = 0, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _jsonl(*rows: dict[str, str]) -> str:
    return "".join(json.dumps(row) + "\n" for row in rows)


def _ok_view(
    *,
    active: set[str] | None = None,
    terminal: set[str] | None = None,
    retained: set[str] | None = None,
) -> WorkspaceIdView:
    return WorkspaceIdView(
        active_ids=frozenset(active or set()),
        terminal_ids=frozenset(terminal or set()),
        retained_ids=frozenset(retained or set()),
        available=True,
    )


@pytest.mark.unit
def test_orphan_resource_to_utc_accepts_naive_datetime() -> None:
    naive = datetime(2026, 5, 7, 14, 15)

    assert orphan_resources._to_utc(naive) == naive.replace(tzinfo=orphan_resources.UTC)


def _run_for(
    *,
    containers: str = "",
    networks: str = "",
    volumes: str = "",
    fail_networks: bool = False,
) -> Any:
    def _run(args: list[str], **_kwargs: object) -> _Completed:
        if args[:3] == ["docker", "ps", "-a"]:
            return _Completed(stdout=containers)
        if args[:3] == ["docker", "network", "ls"]:
            if fail_networks:
                return _Completed(returncode=1, stderr="network list failed")
            return _Completed(stdout=networks)
        if args[:3] == ["docker", "volume", "ls"]:
            return _Completed(stdout=volumes)
        raise AssertionError(f"unexpected subprocess call: {args}")

    return _run


@pytest.mark.unit
def test_orphan_summary_reports_all_resource_kinds(tmp_path: Path) -> None:
    (tmp_path / "git" / "worktrees" / "ws_dead").mkdir(parents=True)
    docker = scan_docker_resources(
        docker_host="unix:///var/run/docker.sock",
        run_subprocess=_run_for(
            containers=_jsonl(
                {
                    "id": "c1",
                    "name": "awf_ws_dead-agent-1",
                    "project": "awf_ws_dead",
                    "service": "agent",
                    "state": "exited",
                    "status": "Exited",
                }
            ),
            networks=_jsonl(
                {
                    "id": "n1",
                    "name": "awf_ws_dead_default",
                    "project": "awf_ws_dead",
                    "driver": "bridge",
                    "scope": "local",
                }
            ),
            volumes=_jsonl(
                {
                    "name": "awf_ws_dead_pgdata",
                    "project": "awf_ws_dead",
                    "driver": "local",
                    "scope": "local",
                }
            ),
        ),
    )
    worktrees = scan_managed_worktrees(tmp_path)

    summary = build_orphan_resource_summary(
        docker_scan=docker,
        worktree_scan=worktrees,
        workspace_view=_ok_view(terminal={"ws_dead"}),
    )
    payload = summary.to_dict()

    assert payload["ok"] is False
    assert payload["status"] == "fail"
    assert payload["reason"] == "ORPHAN_RESOURCES_PRESENT"
    assert payload["resource_count"] == 4
    assert payload["orphan_count"] == 4
    assert payload["orphan_counts_by_kind"] == {
        "container": 1,
        "network": 1,
        "volume": 1,
        "worktree": 1,
    }
    assert payload["orphan_classification_counts"] == {"terminal": 4, "missing": 0}
    assert payload["cleanup_readiness"]["ready"] is False
    assert payload["cleanup_readiness"]["reason"] == "ORPHAN_RESOURCES_PRESENT"
    assert payload["cleanup_readiness"]["dry_run_only"] is True
    assert {example["kind"] for example in payload["examples"]} == {
        "container",
        "network",
        "volume",
        "worktree",
    }


@pytest.mark.unit
def test_active_workspace_resources_are_expected(tmp_path: Path) -> None:
    (tmp_path / "git" / "worktrees" / "ws_live").mkdir(parents=True)
    docker = scan_docker_resources(
        docker_host="unix:///var/run/docker.sock",
        run_subprocess=_run_for(
            containers=_jsonl(
                {
                    "id": "c1",
                    "name": "awf_ws_live-agent-1",
                    "project": "awf_ws_live",
                    "service": "agent",
                    "state": "running",
                    "status": "Up",
                }
            )
        ),
    )

    summary = build_orphan_resource_summary(
        docker_scan=docker,
        worktree_scan=scan_managed_worktrees(tmp_path),
        workspace_view=_ok_view(active={"ws_live"}),
    ).to_dict()

    assert summary["ok"] is True
    assert summary["reason"] == "NO_ORPHANS"
    assert summary["resource_count"] == 2
    assert summary["expected_count"] == 2
    assert "active_count" not in summary
    assert summary["orphan_count"] == 0
    assert summary["cleanup_readiness"]["ready"] is True


@pytest.mark.unit
def test_retained_terminal_workspace_resources_are_not_orphans(tmp_path: Path) -> None:
    (tmp_path / "git" / "worktrees" / "ws_done").mkdir(parents=True)

    summary = build_orphan_resource_summary(
        docker_scan=empty_docker_scan(),
        worktree_scan=scan_managed_worktrees(tmp_path),
        workspace_view=_ok_view(terminal={"ws_done"}, retained={"ws_done"}),
    )
    payload = summary.to_dict()

    assert payload["ok"] is True
    assert payload["reason"] == "NO_ORPHANS"
    assert payload["resource_count"] == 1
    assert payload["expected_count"] == 1
    assert payload["orphan_count"] == 0
    assert summary.records[0].classification == "expected"
    assert summary.records[0].reason == "WORKSPACE_TERMINAL_WITHIN_RETENTION"


@pytest.mark.unit
def test_terminal_workspace_live_container_is_leak_within_retention(tmp_path: Path) -> None:
    docker = scan_docker_resources(
        docker_host="unix:///var/run/docker.sock",
        run_subprocess=_run_for(
            containers=_jsonl(
                {
                    "id": "c1",
                    "name": "awf_ws_zombie-agent-1",
                    "project": "awf_ws_zombie",
                    "service": "agent",
                    "state": "running",
                    "status": "Up 5 minutes",
                }
            )
        ),
    )

    summary = build_orphan_resource_summary(
        docker_scan=docker,
        worktree_scan=scan_managed_worktrees(tmp_path),
        workspace_view=_ok_view(terminal={"ws_zombie"}, retained={"ws_zombie"}),
    )
    payload = summary.to_dict()

    assert payload["ok"] is False
    assert payload["reason"] == "ORPHAN_RESOURCES_PRESENT"
    assert payload["orphan_count"] == 1
    assert summary.records[0].classification == "terminal"
    assert summary.records[0].reason == "WORKSPACE_TERMINAL_LIVE_RUNTIME"
    assert payload["cleanup_readiness"]["ready"] is False


@pytest.mark.unit
def test_terminal_workspace_live_network_is_leak_within_retention(tmp_path: Path) -> None:
    docker = scan_docker_resources(
        docker_host="unix:///var/run/docker.sock",
        run_subprocess=_run_for(
            networks=_jsonl(
                {
                    "id": "n1",
                    "name": "awf_ws_zombie_default",
                    "project": "awf_ws_zombie",
                    "driver": "bridge",
                    "scope": "local",
                }
            )
        ),
    )

    summary = build_orphan_resource_summary(
        docker_scan=docker,
        worktree_scan=scan_managed_worktrees(tmp_path),
        workspace_view=_ok_view(terminal={"ws_zombie"}, retained={"ws_zombie"}),
    )
    payload = summary.to_dict()

    assert payload["ok"] is False
    assert payload["reason"] == "ORPHAN_RESOURCES_PRESENT"
    assert payload["orphan_count"] == 1
    assert summary.records[0].classification == "terminal"
    assert summary.records[0].reason == "WORKSPACE_TERMINAL_LIVE_RUNTIME"
    assert payload["cleanup_readiness"]["ready"] is False


@pytest.mark.unit
def test_terminal_workspace_volume_and_worktree_within_retention_remain_expected(
    tmp_path: Path,
) -> None:
    (tmp_path / "git" / "worktrees" / "ws_done").mkdir(parents=True)
    docker = scan_docker_resources(
        docker_host="unix:///var/run/docker.sock",
        run_subprocess=_run_for(
            volumes=_jsonl(
                {
                    "name": "awf_ws_done_pgdata",
                    "project": "awf_ws_done",
                    "driver": "local",
                    "scope": "local",
                }
            )
        ),
    )

    summary = build_orphan_resource_summary(
        docker_scan=docker,
        worktree_scan=scan_managed_worktrees(tmp_path),
        workspace_view=_ok_view(terminal={"ws_done"}, retained={"ws_done"}),
    )
    payload = summary.to_dict()

    assert payload["ok"] is True
    assert payload["reason"] == "NO_ORPHANS"
    assert payload["resource_count"] == 2
    assert payload["expected_count"] == 2
    assert payload["orphan_count"] == 0
    classifications = {
        (record.kind, record.classification, record.reason) for record in summary.records
    }
    assert classifications == {
        ("volume", "expected", "WORKSPACE_TERMINAL_WITHIN_RETENTION"),
        ("worktree", "expected", "WORKSPACE_TERMINAL_WITHIN_RETENTION"),
    }


@pytest.mark.unit
def test_terminal_workspace_mixed_runtime_and_salvage_partitions_correctly(
    tmp_path: Path,
) -> None:
    (tmp_path / "git" / "worktrees" / "ws_partial").mkdir(parents=True)
    docker = scan_docker_resources(
        docker_host="unix:///var/run/docker.sock",
        run_subprocess=_run_for(
            containers=_jsonl(
                {
                    "id": "c1",
                    "name": "awf_ws_partial-agent-1",
                    "project": "awf_ws_partial",
                    "service": "agent",
                    "state": "running",
                    "status": "Up",
                }
            ),
            networks=_jsonl(
                {
                    "id": "n1",
                    "name": "awf_ws_partial_default",
                    "project": "awf_ws_partial",
                    "driver": "bridge",
                    "scope": "local",
                }
            ),
            volumes=_jsonl(
                {
                    "name": "awf_ws_partial_pgdata",
                    "project": "awf_ws_partial",
                    "driver": "local",
                    "scope": "local",
                }
            ),
        ),
    )

    summary = build_orphan_resource_summary(
        docker_scan=docker,
        worktree_scan=scan_managed_worktrees(tmp_path),
        workspace_view=_ok_view(terminal={"ws_partial"}, retained={"ws_partial"}),
    )
    payload = summary.to_dict()

    assert payload["ok"] is False
    assert payload["reason"] == "ORPHAN_RESOURCES_PRESENT"
    assert payload["resource_count"] == 4
    assert payload["orphan_count"] == 2
    assert payload["expected_count"] == 2
    assert payload["orphan_counts_by_kind"] == {
        "container": 1,
        "network": 1,
        "volume": 0,
        "worktree": 0,
    }
    assert payload["expected_counts_by_kind"] == {
        "container": 0,
        "network": 0,
        "volume": 1,
        "worktree": 1,
    }
    leak_reasons = {
        record.kind: record.reason
        for record in summary.records
        if record.classification == "terminal"
    }
    salvage_reasons = {
        record.kind: record.reason
        for record in summary.records
        if record.classification == "expected"
    }
    assert leak_reasons == {
        "container": "WORKSPACE_TERMINAL_LIVE_RUNTIME",
        "network": "WORKSPACE_TERMINAL_LIVE_RUNTIME",
    }
    assert salvage_reasons == {
        "volume": "WORKSPACE_TERMINAL_WITHIN_RETENTION",
        "worktree": "WORKSPACE_TERMINAL_WITHIN_RETENTION",
    }


@pytest.mark.unit
def test_past_retention_terminal_keeps_existing_behaviour(tmp_path: Path) -> None:
    (tmp_path / "git" / "worktrees" / "ws_old").mkdir(parents=True)
    docker = scan_docker_resources(
        docker_host="unix:///var/run/docker.sock",
        run_subprocess=_run_for(
            containers=_jsonl(
                {
                    "id": "c1",
                    "name": "awf_ws_old-agent-1",
                    "project": "awf_ws_old",
                    "service": "agent",
                    "state": "exited",
                    "status": "Exited",
                }
            ),
            volumes=_jsonl(
                {
                    "name": "awf_ws_old_pgdata",
                    "project": "awf_ws_old",
                    "driver": "local",
                    "scope": "local",
                }
            ),
        ),
    )

    summary = build_orphan_resource_summary(
        docker_scan=docker,
        worktree_scan=scan_managed_worktrees(tmp_path),
        workspace_view=_ok_view(terminal={"ws_old"}),
    )
    payload = summary.to_dict()

    assert payload["ok"] is False
    assert payload["reason"] == "ORPHAN_RESOURCES_PRESENT"
    assert payload["resource_count"] == 3
    assert payload["orphan_count"] == 3
    assert all(record.classification == "terminal" for record in summary.records)
    reasons_by_kind = {record.kind: record.reason for record in summary.records}
    assert reasons_by_kind == {
        "container": "WORKSPACE_TERMINAL_LIVE_RUNTIME",
        "volume": "WORKSPACE_TERMINAL",
        "worktree": "WORKSPACE_TERMINAL",
    }


@pytest.mark.unit
def test_missing_workspace_resources_are_orphans(tmp_path: Path) -> None:
    docker = scan_docker_resources(
        docker_host="unix:///var/run/docker.sock",
        run_subprocess=_run_for(
            volumes=_jsonl(
                {
                    "name": "awf-ws_missing_pgdata",
                    "project": "awf-ws_missing",
                    "driver": "local",
                    "scope": "local",
                }
            )
        ),
    )

    summary = build_orphan_resource_summary(
        docker_scan=docker,
        worktree_scan=scan_managed_worktrees(tmp_path),
        workspace_view=_ok_view(active={"ws_live"}),
    ).to_dict()

    assert summary["status"] == "fail"
    assert summary["orphan_classification_counts"] == {"terminal": 0, "missing": 1}
    assert summary["examples"][0]["workspace_id"] == "ws_missing"
    assert summary["examples"][0]["classification"] == "missing"
    assert summary["examples"][0]["reason"] == "WORKSPACE_MISSING"


@pytest.mark.unit
def test_companion_worktree_scan_uses_parent_workspace_id(tmp_path: Path) -> None:
    worktree_root = tmp_path / "git" / "worktrees"
    (worktree_root / "ws_live").mkdir(parents=True)
    (worktree_root / "ws_live__companion__backend").mkdir()

    scan = scan_managed_worktrees(tmp_path)
    ids = {
        resource.path.rsplit("/", maxsplit=1)[-1]: resource.workspace_id
        for resource in scan.resources
    }

    assert ids == {
        "ws_live": "ws_live",
        "ws_live__companion__backend": "ws_live",
    }
    summary = build_orphan_resource_summary(
        docker_scan=empty_docker_scan(),
        worktree_scan=scan,
        workspace_view=_ok_view(active={"ws_live"}),
    ).to_dict()
    assert summary["reason"] == "NO_ORPHANS"
    assert summary["expected_count"] == 2
    assert summary["expected_counts_by_kind"]["worktree"] == 2


@pytest.mark.unit
def test_db_unavailable_makes_classification_unknown(tmp_path: Path) -> None:
    docker = scan_docker_resources(
        docker_host="unix:///var/run/docker.sock",
        run_subprocess=_run_for(
            containers=_jsonl(
                {
                    "id": "c1",
                    "name": "awf_ws_unknown-agent-1",
                    "project": "awf_ws_unknown",
                    "service": "agent",
                    "state": "running",
                    "status": "Up",
                }
            )
        ),
    )

    summary = build_orphan_resource_summary(
        docker_scan=docker,
        worktree_scan=scan_managed_worktrees(tmp_path),
        workspace_view=WorkspaceIdView(
            active_ids=frozenset(),
            terminal_ids=frozenset(),
            available=False,
        ),
    ).to_dict()

    assert summary["ok"] is True
    assert summary["status"] == "unknown"
    assert summary["reason"] == "DB_UNAVAILABLE"
    assert summary["resource_count"] == 1
    assert summary["unknown_count"] == 1
    assert summary["orphan_count"] == 0
    assert summary["examples"][0]["classification"] == "unknown"
    assert summary["cleanup_readiness"]["ready"] is False
    assert summary["cleanup_readiness"]["dry_run_only"] is True


@pytest.mark.unit
def test_workspace_lookup_returns_unavailable_for_database_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_engine(_url: str) -> object:
        raise SQLAlchemyError("database unavailable")

    monkeypatch.setattr(orphan_resources, "make_engine", fail_engine)

    view = asyncio.run(default_workspace_id_lookup("postgresql+asyncpg://awf@localhost/awf"))

    assert view == WorkspaceIdView(
        active_ids=frozenset(),
        terminal_ids=frozenset(),
        available=False,
    )


@pytest.mark.unit
def test_workspace_lookup_does_not_swallow_programming_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_engine(_url: str) -> object:
        raise RuntimeError("bug in caller")

    monkeypatch.setattr(orphan_resources, "make_engine", fail_engine)

    with pytest.raises(RuntimeError, match="bug in caller"):
        asyncio.run(default_workspace_id_lookup("postgresql+asyncpg://awf@localhost/awf"))


@pytest.mark.unit
def test_workspace_lookup_disposes_engine_after_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Rows:
        def all(self) -> list[tuple[str, str]]:
            return [
                ("ws_active", "running"),
                ("ws_terminal", "failed"),
                ("ws_ignored", "archived"),
            ]

    class _Connection:
        async def __aenter__(self) -> _Connection:
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        async def execute(self, *_args: object, **_kwargs: object) -> _Rows:
            return _Rows()

    class _Engine:
        def __init__(self) -> None:
            self.disposed = False

        def connect(self) -> _Connection:
            return _Connection()

        async def dispose(self) -> None:
            self.disposed = True

    engine = _Engine()
    monkeypatch.setattr(orphan_resources, "make_engine", lambda _url: engine)

    view = asyncio.run(
        default_workspace_id_lookup("postgresql+asyncpg://awf:awf_dev@localhost:5433/awf")
    )

    assert view == WorkspaceIdView(
        active_ids=frozenset({"ws_active"}),
        terminal_ids=frozenset({"ws_terminal"}),
        available=True,
    )
    assert engine.disposed


@pytest.mark.unit
def test_docker_scan_reports_missing_binary_with_structured_reason() -> None:
    def _missing_binary(args: list[str], **_kwargs: object) -> _Completed:
        raise FileNotFoundError(args[0])

    docker = scan_docker_resources(
        docker_host="unix:///var/run/docker.sock",
        run_subprocess=_missing_binary,
    )

    assert docker.ok is False
    assert docker.detail == "docker binary not found on PATH"


@pytest.mark.unit
def test_parse_docker_resource_rows_ignores_malformed_non_dict_and_unmanaged_rows() -> None:
    resources = parse_docker_resource_rows(
        "container",
        "\n"
        "{malformed json}\n"
        "[]\n"
        '{"project":"external_ws_1","id":"external"}\n'
        '{"project":"awf_ws_real","id":"c1","name":"","service":"agent"}\n',
    )

    assert len(resources) == 1
    assert resources[0].workspace_id == "ws_real"
    assert resources[0].id == "c1"
    assert resources[0].name is None
    assert workspace_id_from_project("awf-not-a-workspace") is None
    assert empty_worktree_scan().reason == "WORKTREE_SCAN_OK"


@pytest.mark.unit
def test_legacy_orphan_payload_preserves_db_unavailable_shape() -> None:
    summary = build_orphan_resource_summary(
        docker_scan=ResourceScan(
            ok=True,
            status="ok",
            reason="DOCKER_RESOURCE_SCAN_OK",
            resources=(
                DetectedResource(
                    kind="container",
                    workspace_id="ws_unknown",
                    compose_project="awf_ws_unknown",
                    id="c1",
                    name="awf_ws_unknown-agent-1",
                    service="agent",
                ),
            ),
        ),
        worktree_scan=empty_worktree_scan(),
        workspace_view=WorkspaceIdView(
            active_ids=frozenset(),
            terminal_ids=frozenset(),
            available=False,
        ),
    )

    payload = legacy_orphan_workspaces_payload(summary)

    assert payload["ok"] is True
    assert payload["status"] == "unknown"
    assert payload["reason"] == "DB_UNAVAILABLE"
    assert payload["container_count"] == 1
    assert payload["examples"] == [
        {
            "compose_project": "awf_ws_unknown",
            "workspace_id": "ws_unknown",
            "classification": "unknown",
            "reason": "DB_UNAVAILABLE",
            "containers": [
                {
                    "kind": "container",
                    "workspace_id": "ws_unknown",
                    "classification": "unknown",
                    "reason": "DB_UNAVAILABLE",
                    "compose_project": "awf_ws_unknown",
                    "id": "c1",
                    "name": "awf_ws_unknown-agent-1",
                    "service": "agent",
                }
            ],
        }
    ]


@pytest.mark.unit
@pytest.mark.parametrize(
    ("detail", "reason"),
    [
        ("docker binary not found on PATH", "DOCKER_CLI_NOT_FOUND"),
        ("permission denied", "DOCKER_UNAVAILABLE"),
    ],
)
def test_legacy_orphan_payload_maps_unavailable_scanner_reasons(
    detail: str,
    reason: str,
) -> None:
    summary = build_orphan_resource_summary(
        docker_scan=ResourceScan(
            ok=False,
            status="unavailable",
            reason="DOCKER_RESOURCE_SCAN_UNAVAILABLE",
            detail=detail,
        ),
        worktree_scan=empty_worktree_scan(),
        workspace_view=_ok_view(),
    )

    payload = legacy_orphan_workspaces_payload(summary)

    assert payload["ok"] is True
    assert payload["status"] == "unavailable"
    assert payload["reason"] == reason
    assert payload["orphan_count"] == 0
    assert payload["examples"] == []


@pytest.mark.unit
def test_legacy_orphan_payload_splits_expected_missing_and_terminal_counts() -> None:
    no_orphans = build_orphan_resource_summary(
        docker_scan=ResourceScan(
            ok=True,
            status="ok",
            reason="DOCKER_RESOURCE_SCAN_OK",
            resources=(
                DetectedResource(
                    kind="network",
                    workspace_id="ws_live",
                    compose_project="awf_ws_live",
                ),
            ),
        ),
        worktree_scan=empty_worktree_scan(),
        workspace_view=_ok_view(active={"ws_live"}),
    )

    assert legacy_orphan_workspaces_payload(no_orphans) == {
        "ok": True,
        "status": "ok",
        "reason": "NO_ORPHANS",
        "active_count": 1,
        "orphan_count": 0,
        "examples": [],
    }

    with_orphans = build_orphan_resource_summary(
        docker_scan=ResourceScan(
            ok=True,
            status="ok",
            reason="DOCKER_RESOURCE_SCAN_OK",
            resources=(
                DetectedResource(
                    kind="container",
                    workspace_id="ws_done",
                    compose_project="awf_ws_done",
                    name="awf_ws_done-agent-1",
                ),
                DetectedResource(
                    kind="container",
                    workspace_id="ws_missing",
                    compose_project="awf_ws_missing",
                    name="awf_ws_missing-agent-1",
                ),
            ),
        ),
        worktree_scan=empty_worktree_scan(),
        workspace_view=_ok_view(active={"ws_live"}, terminal={"ws_done"}),
    )

    payload = legacy_orphan_workspaces_payload(with_orphans)

    assert payload["ok"] is False
    assert payload["reason"] == "ORPHANS_PRESENT"
    assert payload["active_count"] == 0
    assert payload["orphan_count"] == 2
    assert payload["orphan_terminal_count"] == 1
    assert payload["orphan_missing_count"] == 1
    assert [example["workspace_id"] for example in payload["examples"]] == [
        "ws_done",
        "ws_missing",
    ]


@pytest.mark.unit
def test_docker_scan_unavailable_is_structured(tmp_path: Path) -> None:
    docker = scan_docker_resources(
        docker_host="unix:///var/run/docker.sock",
        run_subprocess=_run_for(fail_networks=True),
    )

    summary = build_orphan_resource_summary(
        docker_scan=docker,
        worktree_scan=scan_managed_worktrees(tmp_path),
        workspace_view=_ok_view(),
    ).to_dict()

    assert summary["ok"] is True
    assert summary["status"] == "unavailable"
    assert summary["reason"] == "DOCKER_RESOURCE_SCAN_UNAVAILABLE"
    assert summary["scanners"]["docker"]["ok"] is False
    assert "network list failed" in summary["scanners"]["docker"]["detail"]
    assert summary["cleanup_readiness"]["ready"] is False


@pytest.mark.unit
def test_docker_scan_reports_missing_binary() -> None:
    def _missing_binary(args: list[str], **_kwargs: object) -> _Completed:
        raise FileNotFoundError(args[0])

    docker = scan_docker_resources(
        docker_host="unix:///var/run/docker.sock",
        run_subprocess=_missing_binary,
    )

    assert docker.ok is False
    assert docker.status == "unavailable"
    assert docker.reason == "DOCKER_RESOURCE_SCAN_UNAVAILABLE"
    assert docker.detail == "docker binary not found on PATH"
    assert docker.resources == ()


@pytest.mark.unit
def test_docker_scan_timeout_is_structured() -> None:
    def _timeout(args: list[str], **_kwargs: object) -> _Completed:
        raise subprocess.TimeoutExpired(args, timeout=0.01)

    docker = scan_docker_resources(
        docker_host="unix:///var/run/docker.sock",
        run_subprocess=_timeout,
        timeout=0.01,
    )

    assert docker.ok is False
    assert docker.reason == "DOCKER_RESOURCE_SCAN_UNAVAILABLE"
    assert "container" in (docker.detail or "")


@pytest.mark.unit
def test_async_docker_scan_reports_missing_binary() -> None:
    class _MissingRunner:
        async def run(
            self,
            args: list[str],
            *,
            input_bytes: bytes | None = None,
            cwd: str | None = None,
        ) -> Any:
            raise FileNotFoundError(args[0])

    docker = asyncio.run(scan_docker_resources_async(runner=_MissingRunner()))

    assert docker.ok is False
    assert docker.reason == "DOCKER_RESOURCE_SCAN_UNAVAILABLE"
    assert docker.detail == "docker binary not found on PATH"


@pytest.mark.unit
def test_async_docker_scan_collects_timeout_exception_and_nonzero_failures() -> None:
    class _MixedRunner:
        def __init__(self) -> None:
            self.calls = 0

        async def run(
            self,
            args: list[str],
            *,
            input_bytes: bytes | None = None,
            cwd: str | None = None,
        ) -> Any:
            self.calls += 1
            if self.calls == 1:
                await asyncio.sleep(1)
            if self.calls == 2:
                raise RuntimeError("network ls exploded")
            return _Completed(returncode=1, stderr="volume ls failed")

    docker = asyncio.run(scan_docker_resources_async(runner=_MixedRunner(), timeout=0.001))

    assert docker.ok is False
    assert docker.reason == "DOCKER_RESOURCE_SCAN_UNAVAILABLE"
    assert "container: docker resource scan exceeded" in (docker.detail or "")
    assert "network: RuntimeError" in (docker.detail or "")
    assert "volume: volume ls failed" in (docker.detail or "")


@pytest.mark.unit
def test_async_docker_scan_runs_resource_commands_concurrently() -> None:
    class _SlowRunner:
        def __init__(self) -> None:
            self.active = 0
            self.max_active = 0

        async def run(
            self,
            args: list[str],
            *,
            input_bytes: bytes | None = None,
            cwd: str | None = None,
        ) -> Any:
            del args, input_bytes, cwd
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            try:
                await asyncio.sleep(0.01)
                return _Completed(stdout="")
            finally:
                self.active -= 1

    runner = _SlowRunner()

    docker = asyncio.run(scan_docker_resources_async(runner=runner, timeout=0.1))

    assert docker.ok is True
    assert runner.max_active == len(docker_resource_commands())


@pytest.mark.unit
def test_worktree_scanner_ignores_non_workspace_entries(tmp_path: Path) -> None:
    root = tmp_path / "git" / "worktrees"
    (root / "ws_real").mkdir(parents=True)
    (root / "not-a-workspace").mkdir()
    (root / "ws_file").write_text("not a directory", encoding="utf-8")

    worktrees = scan_managed_worktrees(tmp_path)
    summary = build_orphan_resource_summary(
        docker_scan=scan_docker_resources(
            docker_host="unix:///var/run/docker.sock",
            run_subprocess=_run_for(),
        ),
        worktree_scan=worktrees,
        workspace_view=_ok_view(terminal={"ws_real"}),
    ).to_dict()

    assert summary["resource_count"] == 1
    assert summary["examples"][0]["kind"] == "worktree"
    assert summary["examples"][0]["workspace_id"] == "ws_real"
    assert summary["examples"][0]["path"] == str(root / "ws_real")


@pytest.mark.unit
def test_worktree_scan_unavailable_is_structured(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "git" / "worktrees"
    root.mkdir(parents=True)

    def _raise_iterdir(self: Path) -> Any:
        if self == root:
            raise PermissionError("permission denied")
        return original_iterdir(self)

    original_iterdir = Path.iterdir
    monkeypatch.setattr(Path, "iterdir", _raise_iterdir)

    scan = scan_managed_worktrees(tmp_path)

    assert scan.ok is False
    assert scan.reason == "WORKTREE_SCAN_UNAVAILABLE"
    assert "permission denied" in (scan.detail or "")


@pytest.mark.unit
def test_empty_and_not_collected_summaries_are_non_destructive() -> None:
    assert empty_docker_scan().reason == "DOCKER_RESOURCE_SCAN_OK"
    assert empty_worktree_scan().reason == "WORKTREE_SCAN_OK"

    payload = summary_not_collected().to_dict()

    assert payload["ok"] is True
    assert payload["reason"] == "ORPHAN_RESOURCE_SCAN_NOT_COLLECTED"
    assert payload["cleanup_readiness"]["dry_run_only"] is True


@pytest.mark.unit
def test_docker_templates_include_compose_project_labels() -> None:
    for command in docker_resource_commands():
        assert "--filter" in command.args
        assert "label=com.docker.compose.project" in command.args
        fmt = command.args[command.args.index("--format") + 1]
        assert '.Label "com.docker.compose.project"' in fmt


@pytest.mark.unit
def test_parse_docker_resource_rows_ignores_malformed_and_non_awf_rows() -> None:
    resources = parse_docker_resource_rows(
        "container",
        "\n"
        "not json\n"
        "[]\n"
        + json.dumps({"project": "other_ws_ignored", "id": "c1"})
        + "\n"
        + json.dumps({"project": "awf_not_workspace", "id": "c2"})
        + "\n"
        + json.dumps(
            {
                "project": "awf-ws_valid",
                "id": "c3",
                "name": "",
                "service": "agent",
            }
        )
        + "\n",
    )

    assert [resource.workspace_id for resource in resources] == ["ws_valid"]
    assert resources[0].compose_project == "awf-ws_valid"
    assert resources[0].id == "c3"
    assert resources[0].name is None


@pytest.mark.unit
def test_workspace_id_from_project_accepts_only_awf_workspace_projects() -> None:
    assert workspace_id_from_project("awf-ws_dash") == "ws_dash"
    assert workspace_id_from_project("awf_ws_under") == "ws_under"
    assert workspace_id_from_project("not-awf") is None
    assert workspace_id_from_project("awf_not_workspace") is None
    assert workspace_id_from_project("compose_ws_other") is None


@pytest.mark.unit
def test_legacy_payload_reports_db_unavailable_with_container_examples() -> None:
    summary = build_orphan_resource_summary(
        docker_scan=scan_docker_resources(
            docker_host="unix:///var/run/docker.sock",
            run_subprocess=_run_for(
                containers=_jsonl(
                    {
                        "id": "container-1",
                        "name": "awf_ws_unknown-agent-1",
                        "project": "awf_ws_unknown",
                        "service": "agent",
                        "state": "running",
                        "status": "Up",
                    }
                )
            ),
        ),
        worktree_scan=empty_worktree_scan(),
        workspace_view=unavailable_workspace_view(),
    )

    payload = legacy_orphan_workspaces_payload(summary)

    assert payload["ok"] is True
    assert payload["status"] == "unknown"
    assert payload["reason"] == "DB_UNAVAILABLE"
    assert payload["container_count"] == 1
    examples = payload["examples"]
    assert isinstance(examples, list)
    assert examples[0]["compose_project"] == "awf_ws_unknown"
    assert examples[0]["containers"][0]["id"] == "container-1"


@pytest.mark.unit
@pytest.mark.parametrize(
    ("detail", "reason"),
    [
        ("docker binary not found on PATH", "DOCKER_CLI_NOT_FOUND"),
        ("cannot connect to docker daemon", "DOCKER_UNAVAILABLE"),
    ],
)
def test_legacy_payload_preserves_docker_unavailable_reason(
    detail: str,
    reason: str,
) -> None:
    summary = build_orphan_resource_summary(
        docker_scan=ResourceScan(
            ok=False,
            status="unavailable",
            reason="DOCKER_RESOURCE_SCAN_UNAVAILABLE",
            detail=detail,
        ),
        worktree_scan=empty_worktree_scan(),
        workspace_view=_ok_view(),
    )

    payload = legacy_orphan_workspaces_payload(summary)

    assert payload["ok"] is True
    assert payload["status"] == "unavailable"
    assert payload["reason"] == reason
    assert payload["orphan_count"] == 0
    assert payload["examples"] == []


@pytest.mark.unit
def test_legacy_payload_reports_no_orphans_with_expected_workspaces() -> None:
    summary = build_orphan_resource_summary(
        docker_scan=scan_docker_resources(
            docker_host="unix:///var/run/docker.sock",
            run_subprocess=_run_for(
                containers=_jsonl(
                    {
                        "id": "container-1",
                        "name": "awf_ws_live-agent-1",
                        "project": "awf_ws_live",
                    }
                )
            ),
        ),
        worktree_scan=empty_worktree_scan(),
        workspace_view=_ok_view(active={"ws_live"}),
    )

    payload = legacy_orphan_workspaces_payload(summary)

    assert payload == {
        "ok": True,
        "status": "ok",
        "reason": "NO_ORPHANS",
        "active_count": 1,
        "orphan_count": 0,
        "examples": [],
    }


@pytest.mark.unit
def test_legacy_payload_groups_terminal_and_missing_orphan_examples() -> None:
    summary = build_orphan_resource_summary(
        docker_scan=scan_docker_resources(
            docker_host="unix:///var/run/docker.sock",
            run_subprocess=_run_for(
                containers=_jsonl(
                    {
                        "id": "container-1",
                        "name": "awf_ws_done-agent-1",
                        "project": "awf_ws_done",
                    }
                ),
                networks=_jsonl(
                    {
                        "id": "network-1",
                        "name": "awf_ws_missing_default",
                        "project": "awf_ws_missing",
                    }
                ),
                volumes=_jsonl(
                    {
                        "name": "awf_ws_missing_pgdata",
                        "project": "awf_ws_missing",
                    }
                ),
            ),
        ),
        worktree_scan=empty_worktree_scan(),
        workspace_view=_ok_view(terminal={"ws_done"}),
    )

    payload = legacy_orphan_workspaces_payload(summary)

    assert payload["ok"] is False
    assert payload["reason"] == "ORPHANS_PRESENT"
    assert payload["orphan_count"] == 2
    assert payload["orphan_missing_count"] == 1
    assert payload["orphan_terminal_count"] == 1
    examples = payload["examples"]
    assert isinstance(examples, list)
    assert [example["workspace_id"] for example in examples] == ["ws_done", "ws_missing"]
    assert examples[0]["containers"][0]["id"] == "container-1"
    assert examples[1]["containers"] == []


@pytest.mark.unit
def test_workspace_lookup_returns_active_and_terminal_ids(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Rows:
        def all(self) -> list[tuple[str, str]]:
            return [
                ("ws_active", "running"),
                ("ws_live", "ready"),
                ("ws_done", "completed"),
                ("ws_ignored", "archived"),
                ("ws_unknown", "future_status"),
            ]

    class _Connection:
        async def __aenter__(self) -> _Connection:
            return self

        async def __aexit__(self, *_exc_info: object) -> None:
            return None

        async def execute(self, *_args: object, **_kwargs: object) -> _Rows:
            return _Rows()

    class _Engine:
        def __init__(self) -> None:
            self.disposed = False

        def connect(self) -> _Connection:
            return _Connection()

        async def dispose(self) -> None:
            self.disposed = True

    engine = _Engine()
    monkeypatch.setattr(orphan_resources, "make_engine", lambda _url: engine)

    view = asyncio.run(default_workspace_id_lookup("postgresql+asyncpg://awf@localhost/awf"))

    assert view == WorkspaceIdView(
        active_ids=frozenset({"ws_active", "ws_live"}),
        terminal_ids=frozenset({"ws_done"}),
        available=True,
    )
    assert engine.disposed is True


class _RecordingComposeTeardown:
    """Fake compose teardown for the readiness-driven reaper tests."""

    def __init__(self, result: Any | None = None) -> None:
        self.calls: list[tuple[str, Path, str]] = []
        self.remove_volumes_calls: list[bool] = []
        from awf.node.compose_manager import ComposeTeardownResult

        self._result = result or ComposeTeardownResult(
            status="succeeded", reason_code="DOCKER_COMPOSE_DOWN_SUCCEEDED"
        )

    async def __call__(
        self, project_name: str, compose_file: Path, workspace_id: str, remove_volumes: bool
    ) -> Any:
        self.calls.append((project_name, compose_file, workspace_id))
        self.remove_volumes_calls.append(remove_volumes)
        return self._result


def _orphan_summary_with_compose_and_worktree(tmp_path: Path, *, auto_cleanup_orphans: bool) -> Any:
    (tmp_path / "git" / "worktrees" / "ws_dead").mkdir(parents=True)
    docker = scan_docker_resources(
        docker_host="unix:///var/run/docker.sock",
        run_subprocess=_run_for(
            containers=_jsonl(
                {
                    "id": "c1",
                    "name": "awf_ws_dead-agent-1",
                    "project": "awf_ws_dead",
                    "service": "agent",
                    "state": "exited",
                    "status": "Exited",
                }
            )
        ),
    )
    return build_orphan_resource_summary(
        docker_scan=docker,
        worktree_scan=scan_managed_worktrees(tmp_path),
        workspace_view=_ok_view(),  # no rows -> both records are "missing" orphans
        auto_cleanup_orphans=auto_cleanup_orphans,
    )


@pytest.mark.unit
def test_reaper_flag_off_is_dry_run_and_noop(tmp_path: Path) -> None:
    from awf.service.orphan_resources import reap_classified_orphans

    summary = _orphan_summary_with_compose_and_worktree(tmp_path, auto_cleanup_orphans=False)
    assert summary.cleanup_readiness.dry_run_only is True

    teardown = _RecordingComposeTeardown()
    result = asyncio.run(
        reap_classified_orphans(
            summary,
            work_dir=tmp_path,
            compose_teardown=teardown,
            enabled=False,
        )
    )

    assert result.enabled is False
    assert result.status == "disabled"
    assert teardown.calls == []
    assert (tmp_path / "git" / "worktrees" / "ws_dead").exists()


@pytest.mark.unit
def test_reaper_flag_on_reaps_compose_and_worktree(tmp_path: Path) -> None:
    from awf.service.orphan_resources import reap_classified_orphans

    summary = _orphan_summary_with_compose_and_worktree(tmp_path, auto_cleanup_orphans=True)
    assert summary.cleanup_readiness.dry_run_only is False

    teardown = _RecordingComposeTeardown()
    result = asyncio.run(
        reap_classified_orphans(
            summary,
            work_dir=tmp_path,
            compose_teardown=teardown,
            enabled=True,
            min_age_hours=0,  # isolate reap mechanics from the row-less age guard
        )
    )

    assert result.status == "ok"
    assert len(teardown.calls) == 1
    assert teardown.calls[0][0] == "awf_ws_dead"
    assert teardown.calls[0][2] == "ws_dead"
    assert not (tmp_path / "git" / "worktrees" / "ws_dead").exists()
    reaped_kinds = sorted(outcome.kind for outcome in result.reaped)
    assert reaped_kinds == ["compose", "worktree"]
    payload = result.to_dict()
    assert payload["enabled"] is True
    assert payload["status"] == "ok"
    assert {entry["kind"] for entry in payload["reaped"]} == {"compose", "worktree"}


@pytest.mark.unit
def test_reaper_leaves_expected_and_unknown_records(tmp_path: Path) -> None:
    from awf.service.orphan_resources import reap_classified_orphans

    (tmp_path / "git" / "worktrees" / "ws_live").mkdir(parents=True)
    summary = build_orphan_resource_summary(
        docker_scan=empty_docker_scan(),
        worktree_scan=scan_managed_worktrees(tmp_path),
        workspace_view=_ok_view(active={"ws_live"}),
        auto_cleanup_orphans=True,
    )

    teardown = _RecordingComposeTeardown()
    result = asyncio.run(
        reap_classified_orphans(
            summary,
            work_dir=tmp_path,
            compose_teardown=teardown,
            enabled=True,
        )
    )

    assert result.status == "ok"
    assert result.reaped == ()
    assert teardown.calls == []
    assert (tmp_path / "git" / "worktrees" / "ws_live").exists()


@pytest.mark.unit
def test_reaper_skips_when_classification_unknown(tmp_path: Path) -> None:
    from awf.service.orphan_resources import reap_classified_orphans

    summary = build_orphan_resource_summary(
        docker_scan=empty_docker_scan(),
        worktree_scan=scan_managed_worktrees(tmp_path),
        workspace_view=WorkspaceIdView(
            active_ids=frozenset(),
            terminal_ids=frozenset(),
            available=False,
        ),
        auto_cleanup_orphans=True,
    )

    teardown = _RecordingComposeTeardown()
    result = asyncio.run(
        reap_classified_orphans(
            summary,
            work_dir=tmp_path,
            compose_teardown=teardown,
            enabled=True,
        )
    )

    assert result.status == "skipped"
    assert result.reason_code == "ORPHAN_REAP_SKIPPED_UNKNOWN"
    assert teardown.calls == []


@pytest.mark.unit
def test_reaper_skips_when_scanner_unavailable_with_partial_orphans(tmp_path: Path) -> None:
    """A scanner failure that still surfaces partial orphans must not drive reaping.

    When the container list succeeds but the network/volume scan fails,
    ``scan_docker_resources`` returns ``ok=False`` while still carrying the
    listed containers. Those containers classify as orphans, so the summary
    takes the orphan-present (``blocked``) branch rather than the
    scanner-``unknown`` branch -- but the inventory is incomplete, so the reaper
    must still skip rather than tear down stacks on guesswork.
    """
    from awf.service.orphan_resources import reap_classified_orphans

    docker = scan_docker_resources(
        docker_host="unix:///var/run/docker.sock",
        run_subprocess=_run_for(
            containers=_jsonl(
                {
                    "id": "c1",
                    "name": "awf_ws_dead-agent-1",
                    "project": "awf_ws_dead",
                    "service": "agent",
                    "state": "exited",
                    "status": "Exited",
                }
            ),
            fail_networks=True,
        ),
    )
    assert docker.ok is False
    assert docker.resources  # partial inventory: the container survived the failed scan
    summary = build_orphan_resource_summary(
        docker_scan=docker,
        worktree_scan=scan_managed_worktrees(tmp_path),
        workspace_view=_ok_view(),  # no rows -> the listed container is a "missing" orphan
        auto_cleanup_orphans=True,
    )
    assert summary.orphan_count >= 1
    assert summary.cleanup_readiness.status == "blocked"

    teardown = _RecordingComposeTeardown()
    result = asyncio.run(
        reap_classified_orphans(
            summary,
            work_dir=tmp_path,
            compose_teardown=teardown,
            enabled=True,
            min_age_hours=0,
        )
    )

    assert result.status == "skipped"
    assert result.reason_code == "ORPHAN_REAP_SKIPPED_UNKNOWN"
    assert teardown.calls == []


@pytest.mark.unit
def test_reaper_permission_denied_is_loud(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from awf.service.gc_classify import PATH_DELETE_PERMISSION_DENIED
    from awf.service.orphan_resources import reap_classified_orphans

    (tmp_path / "git" / "worktrees" / "ws_dead").mkdir(parents=True)
    summary = build_orphan_resource_summary(
        docker_scan=empty_docker_scan(),
        worktree_scan=scan_managed_worktrees(tmp_path),
        workspace_view=_ok_view(),
        auto_cleanup_orphans=True,
    )

    def _denied(target: object, *, work_dir: Path) -> tuple[bool, str | None, str | None]:
        return False, "permission denied", PATH_DELETE_PERMISSION_DENIED

    monkeypatch.setattr("awf.service.orphan_resources._delete_gc_path", _denied)

    teardown = _RecordingComposeTeardown()
    result = asyncio.run(
        reap_classified_orphans(
            summary,
            work_dir=tmp_path,
            compose_teardown=teardown,
            enabled=True,
            min_age_hours=0,  # isolate the permission-refusal path from the age guard
        )
    )

    assert result.status == "partial"
    assert result.reason_code == "ORPHAN_REAP_PARTIAL"
    assert len(result.errors) == 1
    assert result.errors[0].reason_code == PATH_DELETE_PERMISSION_DENIED


@pytest.mark.unit
def test_reaper_worktree_already_removed_is_idempotent_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from awf.service.gc_classify import PATH_ALREADY_REMOVED
    from awf.service.orphan_resources import reap_classified_orphans

    (tmp_path / "git" / "worktrees" / "ws_dead").mkdir(parents=True)
    summary = build_orphan_resource_summary(
        docker_scan=empty_docker_scan(),
        worktree_scan=scan_managed_worktrees(tmp_path),
        workspace_view=_ok_view(),
        auto_cleanup_orphans=True,
    )

    def _vanished(target: object, *, work_dir: Path) -> tuple[bool, str | None, str | None]:
        return False, None, PATH_ALREADY_REMOVED

    monkeypatch.setattr("awf.service.orphan_resources._delete_gc_path", _vanished)

    teardown = _RecordingComposeTeardown()
    result = asyncio.run(
        reap_classified_orphans(
            summary,
            work_dir=tmp_path,
            compose_teardown=teardown,
            enabled=True,
            min_age_hours=0,  # isolate the idempotent-removal path from the age guard
        )
    )

    assert result.status == "ok"
    assert len(result.reaped) == 1
    assert result.reaped[0].status == "already_removed"


@pytest.mark.unit
def test_build_orphan_compose_teardown_invokes_manager() -> None:
    from awf.node.compose_manager import ComposeTeardownResult
    from awf.service.orphan_resources import build_orphan_compose_teardown

    class _FakeManager:
        def __init__(self) -> None:
            self.calls: list[dict[str, object]] = []

        async def teardown_project(
            self,
            *,
            project_name: str,
            compose_file: Path,
            workspace_id: str,
            remove_volumes: bool = True,
        ) -> ComposeTeardownResult:
            self.calls.append(
                {
                    "project_name": project_name,
                    "compose_file": compose_file,
                    "workspace_id": workspace_id,
                    "remove_volumes": remove_volumes,
                }
            )
            return ComposeTeardownResult(
                status="succeeded", reason_code="DOCKER_COMPOSE_DOWN_SUCCEEDED"
            )

    manager = _FakeManager()
    teardown = build_orphan_compose_teardown(manager)  # type: ignore[arg-type]
    result = asyncio.run(
        teardown("awf_ws_x", Path("/tmp/awf/compose/ws_x/compose.yml"), "ws_x", True)
    )

    assert result.ok
    assert manager.calls[0]["remove_volumes"] is True
    assert manager.calls[0]["project_name"] == "awf_ws_x"

    # The closure forwards the caller's per-workspace decision verbatim: a
    # retained-terminal stack is torn down without deleting its salvage volumes.
    asyncio.run(teardown("awf_ws_y", Path("/tmp/awf/compose/ws_y/compose.yml"), "ws_y", False))
    assert manager.calls[1]["remove_volumes"] is False


def _retained_terminal_runtime_summary(*, retained: bool) -> Any:
    """Summary for a terminal workspace with a leaked container plus a volume.

    When ``retained`` the workspace is still inside its retention window, so
    _classify keeps the volume ``expected`` (salvage evidence) while surfacing
    the live container as ``terminal``. When not retained, both the container
    and the volume classify ``terminal``.
    """

    docker = scan_docker_resources(
        docker_host="unix:///var/run/docker.sock",
        run_subprocess=_run_for(
            containers=_jsonl(
                {
                    "id": "c1",
                    "name": "awf_ws_done-agent-1",
                    "project": "awf_ws_done",
                    "service": "agent",
                    "state": "running",
                    "status": "Up",
                }
            ),
            volumes=_jsonl(
                {
                    "name": "awf_ws_done_pgdata",
                    "project": "awf_ws_done",
                    "driver": "local",
                    "scope": "local",
                }
            ),
        ),
    )
    view = _ok_view(terminal={"ws_done"}, retained={"ws_done"} if retained else None)
    return build_orphan_resource_summary(
        docker_scan=docker,
        worktree_scan=empty_worktree_scan(),
        workspace_view=view,
        auto_cleanup_orphans=True,
    )


@pytest.mark.unit
def test_reaper_preserves_retained_terminal_salvage_volumes(tmp_path: Path) -> None:
    """Reaping a leaked container for a within-retention terminal workspace must
    not pass ``--volumes`` and delete the salvage volume _classify protected."""
    from awf.service.orphan_resources import reap_classified_orphans

    summary = _retained_terminal_runtime_summary(retained=True)
    # The volume is preserved as salvage evidence; only the live container reaps.
    volume_record = next(record for record in summary.records if record.kind == "volume")
    assert volume_record.classification == "expected"
    assert volume_record.reason == "WORKSPACE_TERMINAL_WITHIN_RETENTION"

    teardown = _RecordingComposeTeardown()
    result = asyncio.run(
        reap_classified_orphans(
            summary,
            work_dir=tmp_path,
            compose_teardown=teardown,
            enabled=True,
        )
    )

    assert result.status == "ok"
    assert teardown.calls == [
        ("awf_ws_done", tmp_path / "compose" / "ws_done" / "compose.yml", "ws_done")
    ]
    assert teardown.remove_volumes_calls == [False]


@pytest.mark.unit
def test_reaper_removes_volumes_for_fully_terminal_workspace(tmp_path: Path) -> None:
    """A terminal workspace past its retention window has its volume classified
    ``terminal``, so the stack is torn down with ``--volumes``."""
    from awf.service.orphan_resources import reap_classified_orphans

    summary = _retained_terminal_runtime_summary(retained=False)
    volume_record = next(record for record in summary.records if record.kind == "volume")
    assert volume_record.classification == "terminal"

    teardown = _RecordingComposeTeardown()
    result = asyncio.run(
        reap_classified_orphans(
            summary,
            work_dir=tmp_path,
            compose_teardown=teardown,
            enabled=True,
        )
    )

    assert result.status == "ok"
    assert teardown.remove_volumes_calls == [True]


@pytest.mark.unit
def test_reaper_compose_teardown_failure_is_loud(tmp_path: Path) -> None:
    from awf.node.compose_manager import ComposeTeardownResult
    from awf.service.orphan_resources import reap_classified_orphans

    summary = _orphan_summary_with_compose_and_worktree(tmp_path, auto_cleanup_orphans=True)
    teardown = _RecordingComposeTeardown(
        ComposeTeardownResult(
            status="failed",
            reason_code="DOCKER_UNAVAILABLE",
            error="daemon down",
        )
    )

    result = asyncio.run(
        reap_classified_orphans(
            summary,
            work_dir=tmp_path,
            compose_teardown=teardown,
            enabled=True,
        )
    )

    assert result.status == "partial"
    assert result.reason_code == "ORPHAN_REAP_PARTIAL"
    compose_errors = [outcome for outcome in result.errors if outcome.kind == "compose"]
    assert len(compose_errors) == 1
    assert compose_errors[0].reason_code == "DOCKER_UNAVAILABLE"
    assert compose_errors[0].error == "daemon down"


@pytest.mark.unit
def test_reaper_skips_young_missing_worktree(tmp_path: Path) -> None:
    """A just-created row-less worktree is left alone (possible in-flight provision)."""
    from awf.service.orphan_resources import reap_classified_orphans

    (tmp_path / "git" / "worktrees" / "ws_dead").mkdir(parents=True)
    summary = build_orphan_resource_summary(
        docker_scan=empty_docker_scan(),
        worktree_scan=scan_managed_worktrees(tmp_path),
        workspace_view=_ok_view(),  # no rows -> the worktree is classified "missing"
        auto_cleanup_orphans=True,
    )

    teardown = _RecordingComposeTeardown()
    result = asyncio.run(
        reap_classified_orphans(
            summary,
            work_dir=tmp_path,
            compose_teardown=teardown,
            enabled=True,
            min_age_hours=1.0,
        )
    )

    assert result.status == "ok"
    assert result.reaped == ()
    assert teardown.calls == []
    assert (tmp_path / "git" / "worktrees" / "ws_dead").exists()


@pytest.mark.unit
def test_reaper_reaps_aged_missing_worktree(tmp_path: Path) -> None:
    """An aged row-less worktree (older than the grace window) is reaped."""
    from awf.service.orphan_resources import reap_classified_orphans

    worktree = tmp_path / "git" / "worktrees" / "ws_dead"
    worktree.mkdir(parents=True)
    old = 1_000_000.0
    os.utime(worktree, (old, old))
    summary = build_orphan_resource_summary(
        docker_scan=empty_docker_scan(),
        worktree_scan=scan_managed_worktrees(tmp_path),
        workspace_view=_ok_view(),
        auto_cleanup_orphans=True,
    )

    teardown = _RecordingComposeTeardown()
    result = asyncio.run(
        reap_classified_orphans(
            summary,
            work_dir=tmp_path,
            compose_teardown=teardown,
            enabled=True,
            now=old + 7200.0,  # two hours after the worktree's mtime
            min_age_hours=1.0,
        )
    )

    assert result.status == "ok"
    reaped_kinds = [outcome.kind for outcome in result.reaped]
    assert reaped_kinds == ["worktree"]
    assert not worktree.exists()


@pytest.mark.unit
def test_reaper_reaps_terminal_worktree_without_age_guard(tmp_path: Path) -> None:
    """A fresh worktree backed by a terminal row is reaped despite the grace window."""
    from awf.service.orphan_resources import reap_classified_orphans

    worktree = tmp_path / "git" / "worktrees" / "ws_dead"
    worktree.mkdir(parents=True)
    summary = build_orphan_resource_summary(
        docker_scan=empty_docker_scan(),
        worktree_scan=scan_managed_worktrees(tmp_path),
        workspace_view=_ok_view(terminal={"ws_dead"}),  # terminal row -> not gated
        auto_cleanup_orphans=True,
    )

    teardown = _RecordingComposeTeardown()
    result = asyncio.run(
        reap_classified_orphans(
            summary,
            work_dir=tmp_path,
            compose_teardown=teardown,
            enabled=True,
            min_age_hours=168.0,
        )
    )

    assert result.status == "ok"
    assert [outcome.kind for outcome in result.reaped] == ["worktree"]
    assert not worktree.exists()


@pytest.mark.unit
def test_reaper_skips_young_missing_compose_when_dir_present(tmp_path: Path) -> None:
    """A row-less compose stack with a fresh on-disk compose dir is not torn down."""
    from awf.service.orphan_resources import reap_classified_orphans

    (tmp_path / "compose" / "ws_dead").mkdir(parents=True)
    docker = scan_docker_resources(
        docker_host="unix:///var/run/docker.sock",
        run_subprocess=_run_for(
            containers=_jsonl(
                {
                    "id": "c1",
                    "name": "awf_ws_dead-agent-1",
                    "project": "awf_ws_dead",
                    "service": "agent",
                    "state": "exited",
                    "status": "Exited",
                }
            )
        ),
    )
    summary = build_orphan_resource_summary(
        docker_scan=docker,
        worktree_scan=empty_worktree_scan(),
        workspace_view=_ok_view(),  # no rows -> the container is classified "missing"
        auto_cleanup_orphans=True,
    )

    teardown = _RecordingComposeTeardown()
    result = asyncio.run(
        reap_classified_orphans(
            summary,
            work_dir=tmp_path,
            compose_teardown=teardown,
            enabled=True,
            min_age_hours=1.0,
        )
    )

    assert result.status == "ok"
    assert teardown.calls == []
    assert result.reaped == ()
