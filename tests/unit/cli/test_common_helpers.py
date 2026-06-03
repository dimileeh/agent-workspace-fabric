"""Focused coverage for shared CLI helper branches."""

from __future__ import annotations

import subprocess
from types import SimpleNamespace

import httpx
import pytest
import typer
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.cli import common as cli_common


@pytest.mark.unit
def test_request_context_handles_response_without_request() -> None:
    assert cli_common._request_context(httpx.Response(200)) == (None, None)


@pytest.mark.unit
def test_profile_summary_helpers_cover_empty_runtime_and_scalar_edges() -> None:
    assert cli_common._profile_runtime_summary({}) == ""  # noqa: SLF001
    assert cli_common._has_positive_coverage_target(object())  # noqa: SLF001
    assert cli_common._format_coverage_target("99.5") == "99.5"  # noqa: SLF001


@pytest.mark.unit
@pytest.mark.parametrize(
    ("candidate", "status", "reason_code"),
    [
        (
            SimpleNamespace(compose=SimpleNamespace(path=None), workspace_id="ws"),
            "failed",
            "DOCKER_COMPOSE_DOWN_FAILED",
        ),
        (
            SimpleNamespace(compose=SimpleNamespace(path=object()), workspace_id="ws"),
            "failed",
            "DOCKER_COMPOSE_DOWN_FAILED",
        ),
        (
            SimpleNamespace(compose=SimpleNamespace(path=None)),
            "failed",
            "DOCKER_COMPOSE_DOWN_FAILED",
        ),
    ],
)
def test_compose_teardown_rejects_unusable_candidates(
    candidate: SimpleNamespace,
    status: str,
    reason_code: str,
) -> None:
    result = cli_common._run_terminal_workspace_compose_teardown(candidate)

    assert result.status == status
    assert result.reason_code == reason_code


@pytest.mark.unit
def test_compose_teardown_fails_when_directory_has_no_compose_file(tmp_path) -> None:
    compose_dir = tmp_path / "compose"
    compose_dir.mkdir()
    candidate = SimpleNamespace(
        compose=SimpleNamespace(path=compose_dir),
        workspace_id="ws_missing_compose",
    )

    result = cli_common._run_terminal_workspace_compose_teardown(candidate)

    assert result.status == "failed"
    assert result.reason_code == "DOCKER_COMPOSE_DOWN_FAILED"
    assert result.error == "compose stack file not found"


@pytest.mark.unit
def test_compose_teardown_surfaces_process_failures(monkeypatch, tmp_path) -> None:
    compose_file = tmp_path / "compose.yml"
    compose_file.write_text("services: {}\n", encoding="utf-8")
    candidate = SimpleNamespace(compose=SimpleNamespace(path=compose_file), workspace_id="ws")

    def _run(args: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args, returncode=2, stdout="out", stderr="bad compose")

    monkeypatch.setattr(cli_common.subprocess, "run", _run)

    result = cli_common._run_terminal_workspace_compose_teardown(candidate)

    assert result.status == "failed"
    assert result.reason_code == "DOCKER_COMPOSE_DOWN_FAILED"
    assert result.error == "bad compose"


@pytest.mark.unit
def test_compose_teardown_surfaces_os_errors(monkeypatch, tmp_path) -> None:
    compose_file = tmp_path / "compose.yml"
    compose_file.write_text("services: {}\n", encoding="utf-8")
    candidate = SimpleNamespace(compose=SimpleNamespace(path=compose_file), workspace_id="ws")

    def _run(_args: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        raise OSError("docker missing")

    monkeypatch.setattr(cli_common.subprocess, "run", _run)

    result = cli_common._run_terminal_workspace_compose_teardown(candidate)

    assert result.status == "failed"
    assert result.reason_code == "DOCKER_COMPOSE_DOWN_FAILED"
    assert result.error == "docker missing"


@pytest.mark.unit
def test_compose_teardown_surfaces_timeouts(monkeypatch, tmp_path) -> None:
    compose_file = tmp_path / "compose.yml"
    compose_file.write_text("services: {}\n", encoding="utf-8")
    candidate = SimpleNamespace(compose=SimpleNamespace(path=compose_file), workspace_id="ws")

    def _run(_args: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired("docker compose down", timeout=60)

    monkeypatch.setattr(cli_common.subprocess, "run", _run)

    result = cli_common._run_terminal_workspace_compose_teardown(candidate)

    assert result.status == "failed"
    assert result.reason_code == "DOCKER_COMPOSE_DOWN_FAILED"
    assert result.error == "docker compose down timed out after 60s"


@pytest.mark.unit
def test_compose_teardown_covers_skipped_and_success_paths(monkeypatch, tmp_path) -> None:
    overlay_teardowns: list[tuple[object, str]] = []

    class FakeSettings:
        work_dir = tmp_path / "service"

    monkeypatch.setattr(
        "awf.service.config.resolve_service_settings",
        lambda: FakeSettings(),
    )
    monkeypatch.setattr(
        "awf.node.auth_mounts.teardown_workspace_auth_overlay",
        lambda *, work_dir, workspace_id: overlay_teardowns.append((work_dir, workspace_id)),
    )

    missing = SimpleNamespace(
        compose_file_path=tmp_path / "missing.yml",
        workspace_id="ws_missing",
    )
    # An ``ok`` (skipped, no compose stack) outcome still unmounts the overlay
    # before GC removes the auth dir.
    assert cli_common._run_terminal_workspace_compose_teardown(missing).status == "skipped"
    assert overlay_teardowns == [(tmp_path / "service", "ws_missing")]
    overlay_teardowns.clear()

    compose_file = tmp_path / "compose.yml"
    compose_file.write_text("services: {}\n", encoding="utf-8")
    seen: dict[str, object] = {}

    def _run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        seen["args"] = args
        seen["kwargs"] = kwargs
        return subprocess.CompletedProcess(args, returncode=0, stdout="ok", stderr="")

    monkeypatch.setattr(cli_common.subprocess, "run", _run)

    result = cli_common._run_terminal_workspace_compose_teardown(
        SimpleNamespace(
            compose_file_path=str(compose_file),
            workspace_id="ws_success",
            compose_project_name="custom_project",
        )
    )

    assert result.status == "succeeded"
    assert result.reason_code == "DOCKER_COMPOSE_DOWN_SUCCEEDED"
    # The overlay is unmounted after a successful compose down, before removal.
    assert overlay_teardowns == [(tmp_path / "service", "ws_success")]
    assert seen["args"] == [
        "docker",
        "compose",
        "-p",
        "custom_project",
        "-f",
        str(compose_file),
        "down",
        "--remove-orphans",
    ]
    assert seen["kwargs"] == {
        "check": False,
        "capture_output": True,
        "text": True,
        "timeout": 60,
    }


@pytest.mark.unit
def test_compose_teardown_keeps_result_when_overlay_unmount_raises(monkeypatch, tmp_path) -> None:
    compose_file = tmp_path / "compose.yml"
    compose_file.write_text("services: {}\n", encoding="utf-8")

    class FakeSettings:
        work_dir = tmp_path / "service"

    def _raise(*, work_dir: object, workspace_id: str) -> None:
        raise OSError("device busy")

    monkeypatch.setattr(
        "awf.service.config.resolve_service_settings",
        lambda: FakeSettings(),
    )
    monkeypatch.setattr("awf.node.auth_mounts.teardown_workspace_auth_overlay", _raise)
    monkeypatch.setattr(
        cli_common.subprocess,
        "run",
        lambda args, **_kwargs: subprocess.CompletedProcess(args, returncode=0),
    )

    # A failed overlay unmount is logged but never alters the compose-down result.
    result = cli_common._run_terminal_workspace_compose_teardown(
        SimpleNamespace(compose_file_path=str(compose_file), workspace_id="ws_busy")
    )
    assert result.status == "succeeded"
    assert result.reason_code == "DOCKER_COMPOSE_DOWN_SUCCEEDED"


@pytest.mark.unit
def test_teardown_overlay_skips_when_workspace_id_missing(monkeypatch) -> None:
    def _unexpected_settings() -> object:
        raise AssertionError("settings must not be resolved without a workspace id")

    monkeypatch.setattr("awf.service.config.resolve_service_settings", _unexpected_settings)

    # No workspace_id → short-circuit before resolving settings or unmounting.
    cli_common._teardown_terminal_workspace_auth_overlay(SimpleNamespace())


class _FakeSession:
    def __init__(self, workspace: object | None) -> None:
        self.workspace = workspace

    async def __aenter__(self) -> _FakeSession:
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    async def get(self, *_args: object) -> object | None:
        return self.workspace


def _session_factory(workspace: object | None) -> async_sessionmaker[AsyncSession]:
    def _factory() -> _FakeSession:
        return _FakeSession(workspace)

    return _factory  # type: ignore[return-value]


@pytest.mark.unit
async def test_worktree_remove_covers_skip_success_and_failure(monkeypatch, tmp_path) -> None:
    no_id = await cli_common._run_terminal_workspace_worktree_remove(
        SimpleNamespace(),
        session_factory=_session_factory(None),
    )
    assert no_id.reason_code == "NO_WORKSPACE_ID"

    no_repo = await cli_common._run_terminal_workspace_worktree_remove(
        SimpleNamespace(workspace_id="ws_no_repo"),
        session_factory=_session_factory(SimpleNamespace(repo_url="")),
    )
    assert no_repo.reason_code == "NO_REPO_URL"

    removed: list[tuple[str, str]] = []

    class FakeSettings:
        work_dir = tmp_path

    class FakeGitManager:
        def __init__(self, path: object) -> None:
            self.path = path

        async def remove_worktree(self, *, workspace_id: str, repo_url: str) -> None:
            removed.append((workspace_id, repo_url))

    monkeypatch.setattr(
        "awf.service.config.resolve_service_settings",
        lambda: FakeSettings(),
    )
    monkeypatch.setattr("awf.node.git_manager.GitManager", FakeGitManager)

    success = await cli_common._run_terminal_workspace_worktree_remove(
        SimpleNamespace(workspace_id="ws_success"),
        session_factory=_session_factory(SimpleNamespace(repo_url="git@example.com/app.git")),
    )
    assert success.reason_code == "WORKTREE_REMOVE_SUCCEEDED"
    assert removed == [("ws_success", "git@example.com/app.git")]

    class FailingGitManager(FakeGitManager):
        async def remove_worktree(self, *, workspace_id: str, repo_url: str) -> None:
            raise RuntimeError(f"cannot remove {workspace_id} from {repo_url}")

    monkeypatch.setattr("awf.node.git_manager.GitManager", FailingGitManager)
    failed = await cli_common._run_terminal_workspace_worktree_remove(
        SimpleNamespace(workspace_id="ws_failure"),
        session_factory=_session_factory(SimpleNamespace(repo_url="git@example.com/app.git")),
    )
    assert failed.reason_code == "GIT_WORKTREE_REMOVE_FAILED"
    assert failed.error is not None
    assert "cannot remove ws_failure" in failed.error


@pytest.mark.unit
def test_emit_profile_preview_pretty_covers_nested_summaries(capsys) -> None:
    payload = {
        "profile": {
            "name": "detected",
            "source": "template",
            "confidence": "high",
            "runtime": {"environment": {"PYTHONUNBUFFERED": "1"}},
            "services": [{"name": "postgres"}, "redis"],
            "phases": {
                "setup": [{"command": "uv sync"}, "not-a-map"],
                "validate": ["pytest -q"],
            },
            "validation": {"coverage": {"target": 0.99}},
        },
        "network_posture": {"status": "open", "reason": "bootstrap"},
        "lint_findings": [
            {"severity": "warn", "message": "first"},
            "ignored",
            {"message": "second"},
            {"severity": "info", "message": "third"},
        ],
        "reason": "ready",
    }

    cli_common._emit_profile_preview_pretty(payload)

    output = capsys.readouterr().out
    assert "Runtime: environment=1 value(s)" in output
    assert "Services: postgres, redis" in output
    assert "Setup: uv sync" in output
    assert "Coverage target: 99.0%" in output
    assert "Network posture: open (bootstrap)" in output
    assert "Profile lint: 4 finding(s)" in output
    assert "Reason: ready" in output


@pytest.mark.unit
def test_emit_profile_preview_pretty_covers_clean_and_string_variants(capsys) -> None:
    cli_common._emit_profile_preview_pretty(
        {
            "profile": {
                "name": "plain",
                "runtime": {"image": "python:3.12"},
                "services": [],
                "phases": {},
                "validation": {"coverage": {"minimum_percent": 0}},
            },
            "network_posture": "restricted",
            "lint_findings": [],
        }
    )

    output = capsys.readouterr().out
    assert "Runtime: image=python:3.12" in output
    assert "Services: none declared" in output
    assert "Validation: none declared" in output
    assert "Network posture: restricted" in output
    assert "Profile lint: clean" in output


@pytest.mark.unit
def test_emit_smoke_pretty_covers_links_phases_and_next_actions(capsys) -> None:
    cli_common._emit_smoke_pretty(
        {
            "status": "warn",
            "mode": "mocked",
            "project": "demo",
            "console_links": {"ui": "http://localhost:3000", "api_docs": "http://api/docs"},
            "phases": [
                {
                    "status": "fail",
                    "name": "validate",
                    "message": "missing pytest",
                    "reason_code": "TOOL_MISSING",
                    "action": "install pytest",
                },
                "ignored",
            ],
            "next_actions": ["No action required.", "awf init ."],
        }
    )

    output = capsys.readouterr().out
    assert "Console: http://localhost:3000" in output
    assert "API docs: http://api/docs" in output
    assert "[fail] validate: missing pytest" in output
    assert "reason: TOOL_MISSING" in output
    assert "action: install pytest" in output
    assert "awf init ." in output


@pytest.mark.unit
def test_emit_helpers_cover_scalar_and_mapping_edges(capsys) -> None:
    cli_common._emit(
        {"outer": {"inner": 1}, "items": [{"name": "first"}]}, cli_common.OutputFormat.pretty
    )
    assert (
        cli_common._profile_runtime_summary({"runtime": {"nested": {"a": 1}}})
        == "nested=1 value(s)"
    )
    assert (
        cli_common._profile_runtime_summary({"runtime": {"items": ["a", "b"]}}) == "items=2 item(s)"
    )
    assert cli_common._profile_runtime_summary({"runtime": {"empty": []}}) == "default"
    assert cli_common._profile_coverage_target({"target": ""}) is None
    assert cli_common._format_coverage_target(0.75, fractional=True) == "75.0%"

    output = capsys.readouterr().out
    assert "outer.inner: 1" in output
    assert "items[0].name: first" in output


@pytest.mark.unit
def test_parse_json_option_rejects_invalid_and_non_object(capsys) -> None:
    with pytest.raises(typer.Exit) as invalid:
        cli_common._parse_json_option("--metadata", "{")
    with pytest.raises(typer.Exit) as non_object:
        cli_common._parse_json_option("--metadata", "[]")

    assert invalid.value.exit_code == 2
    assert non_object.value.exit_code == 2
    assert "must be valid JSON" in capsys.readouterr().err


@pytest.mark.unit
def test_handle_response_covers_empty_pretty_items_and_scalar_emit(capsys) -> None:
    cli_common._handle_response(httpx.Response(204), cli_common.OutputFormat.pretty)
    cli_common._handle_response(
        httpx.Response(200, json={"items": [{"id": "one"}]}),
        cli_common.OutputFormat.pretty,
        pretty_items=True,
    )
    cli_common._emit("plain", cli_common.OutputFormat.pretty)

    output = capsys.readouterr().out
    assert "--- #1 ---" in output
    assert "id: one" in output
    assert "plain" in output
