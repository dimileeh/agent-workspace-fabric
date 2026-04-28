"""Orphan AWF Docker resource and worktree detection."""

from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.exc import SQLAlchemyError

from awf.service import orphan_resources
from awf.service.orphan_resources import (
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


def _ok_view(*, active: set[str] | None = None, terminal: set[str] | None = None) -> WorkspaceIdView:
    return WorkspaceIdView(
        active_ids=frozenset(active or set()),
        terminal_ids=frozenset(terminal or set()),
        available=True,
    )


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
    def _missing_binary(_args: list[str], **_kwargs: object) -> _Completed:
        raise FileNotFoundError("docker")

    docker = scan_docker_resources(
        docker_host="unix:///var/run/docker.sock",
        run_subprocess=_missing_binary,
    )

    assert docker.ok is False
    assert docker.status == "unavailable"
    assert docker.detail == "docker binary not found on PATH"


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
def test_parse_docker_rows_skips_invalid_non_awf_and_non_workspace_projects() -> None:
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
    assert resources[0].name is None
    assert workspace_id_from_project("not-awf") is None
    assert workspace_id_from_project("awf_not_workspace") is None
    assert workspace_id_from_project("awf-ws_dash") == "ws_dash"


@pytest.mark.unit
def test_legacy_orphan_payloads_cover_unknown_unavailable_ok_and_orphan_grouping(
    tmp_path: Path,
) -> None:
    unknown_summary = build_orphan_resource_summary(
        docker_scan=scan_docker_resources(
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
        ),
        worktree_scan=empty_worktree_scan(),
        workspace_view=unavailable_workspace_view(),
    )
    unknown = legacy_orphan_workspaces_payload(unknown_summary)
    assert unknown["reason"] == "DB_UNAVAILABLE"
    assert unknown["container_count"] == 1
    assert unknown["examples"][0]["containers"][0]["id"] == "c1"

    unavailable_summary = build_orphan_resource_summary(
        docker_scan=ResourceScan(
            ok=False,
            status="unavailable",
            reason="DOCKER_RESOURCE_SCAN_UNAVAILABLE",
            detail="docker binary not found on PATH",
        ),
        worktree_scan=empty_worktree_scan(),
        workspace_view=_ok_view(),
    )
    unavailable = legacy_orphan_workspaces_payload(unavailable_summary)
    assert unavailable["reason"] == "DOCKER_CLI_NOT_FOUND"
    assert unavailable["orphan_count"] == 0

    ok_summary = build_orphan_resource_summary(
        docker_scan=scan_docker_resources(
            docker_host="unix:///var/run/docker.sock",
            run_subprocess=_run_for(
                containers=_jsonl(
                    {
                        "id": "c2",
                        "name": "awf_ws_live-agent-1",
                        "project": "awf_ws_live",
                    }
                )
            ),
        ),
        worktree_scan=empty_worktree_scan(),
        workspace_view=_ok_view(active={"ws_live"}),
    )
    ok_payload = legacy_orphan_workspaces_payload(ok_summary)
    assert ok_payload["reason"] == "NO_ORPHANS"
    assert ok_payload["active_count"] == 1

    orphan_summary = build_orphan_resource_summary(
        docker_scan=scan_docker_resources(
            docker_host="unix:///var/run/docker.sock",
            run_subprocess=_run_for(
                containers=_jsonl(
                    {
                        "id": "c3",
                        "name": "awf_ws_dead-agent-1",
                        "project": "awf_ws_dead",
                        "service": "agent",
                    }
                ),
                networks=_jsonl(
                    {
                        "id": "n1",
                        "name": "awf_ws_missing_default",
                        "project": "awf_ws_missing",
                    }
                ),
            ),
        ),
        worktree_scan=scan_managed_worktrees(tmp_path),
        workspace_view=_ok_view(terminal={"ws_dead"}),
    )
    orphan_payload = legacy_orphan_workspaces_payload(orphan_summary)
    assert orphan_payload["reason"] == "ORPHANS_PRESENT"
    assert orphan_payload["orphan_terminal_count"] == 1
    assert orphan_payload["orphan_missing_count"] == 1
    dead = next(
        example
        for example in orphan_payload["examples"]
        if example["workspace_id"] == "ws_dead"
    )
    assert dead["containers"][0]["id"] == "c3"
    missing = next(
        example
        for example in orphan_payload["examples"]
        if example["workspace_id"] == "ws_missing"
    )
    assert missing["containers"] == []


@pytest.mark.unit
def test_default_workspace_lookup_disposes_successful_engine(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    disposed = False

    class _Rows:
        def all(self) -> list[tuple[str, str]]:
            return [
                ("ws_active", "running"),
                ("ws_done", "completed"),
                ("ws_unknown", "future_status"),
            ]

    class _Connection:
        async def __aenter__(self) -> _Connection:
            return self

        async def __aexit__(self, *_exc: object) -> None:
            return None

        async def execute(self, *_args: object, **_kwargs: object) -> _Rows:
            return _Rows()

    class _Engine:
        def connect(self) -> _Connection:
            return _Connection()

        async def dispose(self) -> None:
            nonlocal disposed
            disposed = True

    monkeypatch.setattr(orphan_resources, "make_engine", lambda _url: _Engine())

    view = asyncio.run(default_workspace_id_lookup("sqlite+aiosqlite:///fake.db"))

    assert view.active_ids == frozenset({"ws_active"})
    assert view.terminal_ids == frozenset({"ws_done"})
    assert disposed is True
