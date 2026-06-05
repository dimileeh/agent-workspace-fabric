"""Focused tests for project-profile MCP setup tools."""

from __future__ import annotations

import logging
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

from awf.mcp.server import build_mcp_server
from tests.unit.mcp.setup_tools_test_helpers import _json_text, _payload, _settings


@pytest.mark.unit
async def test_initialize_project_profile_uses_onboarding_writer(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from awf.mcp import setup_tools

    project = tmp_path / "repo"
    project.mkdir()
    preview = SimpleNamespace(
        path=project,
        draft=SimpleNamespace(template="python"),
        to_dict=lambda: {
            "path": str(project),
            "inspection": {"detected_template": "python"},
            "draft": {"template": "python", "yaml": "name: python\n"},
            "diagnostics": {},
        },
    )
    writes: list[tuple[Any, bool]] = []

    def fake_preview(path: Path, *, template: str, include_smoke_request: bool) -> Any:
        assert path == project.resolve()
        assert template == "python"
        assert include_smoke_request is True
        return preview

    def fake_write(item: Any, *, force: bool) -> Path:
        writes.append((item, force))
        return project / ".awf" / "workspace.yml"

    monkeypatch.setattr(setup_tools, "preview_project_onboarding", fake_preview)
    monkeypatch.setattr(setup_tools, "write_workspace_profile", fake_write)
    mcp = build_mcp_server(service=MagicMock(), settings=_settings(tmp_path))

    preview_result = _payload(
        await mcp.call_tool(
            "awf_initialize_project_profile",
            {
                "project_path": str(project),
                "template": "python",
                "include_smoke_request": True,
            },
        )
    )
    write_result = _payload(
        await mcp.call_tool(
            "awf_initialize_project_profile",
            {
                "project_path": str(project),
                "template": "python",
                "include_smoke_request": True,
                "write_profile": True,
                "force": True,
            },
        )
    )

    assert preview_result["mode"] == "preview"
    assert "written_path" not in preview_result
    assert writes == [(preview, True)]
    assert write_result["mode"] == "write"
    assert write_result["written_path"].endswith(".awf/workspace.yml")


@pytest.mark.unit
async def test_initialize_project_profile_file_exists_is_structured_mcp_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from awf.mcp import setup_tools

    project = tmp_path / "repo"
    project.mkdir()
    preview = SimpleNamespace(
        path=project,
        draft=SimpleNamespace(template="generic"),
        to_dict=lambda: {
            "path": str(project),
            "inspection": {"detected_template": "generic"},
            "draft": {"template": "generic", "yaml": "name: generic\n"},
            "diagnostics": {},
        },
    )
    monkeypatch.setattr(setup_tools, "preview_project_onboarding", lambda *_a, **_k: preview)

    def fail_write(_preview: Any, *, force: bool) -> Path:
        raise FileExistsError(17, "File exists", project / ".awf" / "workspace.yml")

    monkeypatch.setattr(setup_tools, "write_workspace_profile", fail_write)
    mcp = build_mcp_server(service=MagicMock(), settings=_settings(tmp_path))

    result = await mcp.call_tool(
        "awf_initialize_project_profile",
        {"project_path": str(project), "write_profile": True},
    )
    payload = _payload(result)
    rendered = _json_text(result)

    assert result.isError is True
    assert payload["error_code"] == "PROJECT_PROFILE_EXISTS"
    assert payload["message"] == "project profile already exists; pass force=true to overwrite"
    assert payload["detail"]["project_path"] == str(project.resolve())
    assert "[Errno" not in rendered


@pytest.mark.unit
async def test_initialize_project_profile_file_exists_with_force_has_non_contradictory_message(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from awf.mcp import setup_tools

    project = tmp_path / "repo"
    project.mkdir()
    preview = SimpleNamespace(
        path=project,
        draft=SimpleNamespace(template="generic"),
        to_dict=lambda: {
            "path": str(project),
            "inspection": {"detected_template": "generic"},
            "draft": {"template": "generic", "yaml": "name: generic\n"},
            "diagnostics": {},
        },
    )
    monkeypatch.setattr(setup_tools, "preview_project_onboarding", lambda *_a, **_k: preview)

    def fail_write(_preview: Any, *, force: bool) -> Path:
        assert force is True
        raise FileExistsError(17, "File exists", project / ".awf" / "workspace.yml")

    monkeypatch.setattr(setup_tools, "write_workspace_profile", fail_write)
    mcp = build_mcp_server(service=MagicMock(), settings=_settings(tmp_path))

    result = await mcp.call_tool(
        "awf_initialize_project_profile",
        {"project_path": str(project), "write_profile": True, "force": True},
    )
    payload = _payload(result)
    rendered = _json_text(result)

    assert result.isError is True
    assert payload["error_code"] == "PROJECT_PROFILE_EXISTS"
    assert payload["message"] == "project profile already exists and could not be overwritten"
    assert payload["detail"] == {
        "project_path": str(project.resolve()),
        "force": True,
    }
    assert "pass force=true" not in rendered
    assert "[Errno" not in rendered


@pytest.mark.unit
@pytest.mark.parametrize("exc_type", [RuntimeError, ValueError])
async def test_initialize_project_profile_write_runtime_and_value_errors_are_structured(
    exc_type: type[Exception],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from awf.mcp import setup_tools

    project = tmp_path / "repo"
    project.mkdir()
    raw_token = "sk-proj-" + "f" * 40
    leaked_detail = f"{project}/draft.yaml contains {raw_token}"
    preview = SimpleNamespace(
        path=project,
        draft=SimpleNamespace(template="generic"),
        to_dict=lambda: {
            "path": str(project),
            "inspection": {"detected_template": "generic"},
            "draft": {"template": "generic", "yaml": "name: generic\n"},
            "diagnostics": {},
        },
    )
    monkeypatch.setattr(setup_tools, "preview_project_onboarding", lambda *_a, **_k: preview)

    def fail_write(_preview: Any, *, force: bool) -> Path:
        _ = force
        raise exc_type(leaked_detail)

    monkeypatch.setattr(setup_tools, "write_workspace_profile", fail_write)
    mcp = build_mcp_server(service=MagicMock(), settings=_settings(tmp_path))

    result = await mcp.call_tool(
        "awf_initialize_project_profile",
        {"project_path": str(project), "write_profile": True, "force": True},
    )
    payload = _payload(result)
    rendered = _json_text(result)

    assert result.isError is True
    assert payload["error_code"] == "PROJECT_INIT_FAILED"
    assert payload["message"] == f"could not write project profile: {exc_type.__name__}"
    assert payload["detail"] == {
        "project_path": str(project.resolve()),
        "force": True,
    }
    assert leaked_detail not in rendered
    assert raw_token not in rendered


@pytest.mark.unit
async def test_initialize_project_profile_path_expanduser_failure_returns_structured_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from awf.mcp import setup_tools

    project_path = "missing project"
    expected_project_path = tmp_path / project_path
    leaked_detail = "home directory unavailable"
    original_expanduser = setup_tools.Path.expanduser

    def fail_expanduser(path: Path) -> Path:
        if str(path) == project_path:
            raise RuntimeError(leaked_detail)
        return original_expanduser(path)

    monkeypatch.chdir(tmp_path)
    mcp = build_mcp_server(service=MagicMock(), settings=_settings(tmp_path))
    monkeypatch.setattr(setup_tools.Path, "expanduser", fail_expanduser)

    result = await mcp.call_tool(
        "awf_initialize_project_profile",
        {"project_path": project_path, "template": "generic"},
    )
    payload = _payload(result)
    rendered = _json_text(result)

    assert result.isError is True
    assert payload["error_code"] == "PROJECT_INIT_INVALID_PATH"
    assert payload["message"] == "project path does not exist"
    assert payload["detail"] == {"project_path": str(expected_project_path)}
    assert leaked_detail not in rendered


@pytest.mark.unit
async def test_initialize_project_profile_path_resolve_failure_returns_structured_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from awf.mcp import setup_tools

    project_path = "missing project"
    expected_project_path = tmp_path / project_path
    leaked_detail = "path resolution unavailable"
    original_resolve = setup_tools.Path.resolve

    def fail_resolve(path: Path, *args: Any, **kwargs: Any) -> Path:
        if str(path) == project_path:
            raise OSError(leaked_detail)
        return original_resolve(path, *args, **kwargs)

    monkeypatch.chdir(tmp_path)
    mcp = build_mcp_server(service=MagicMock(), settings=_settings(tmp_path))
    monkeypatch.setattr(setup_tools.Path, "resolve", fail_resolve)

    result = await mcp.call_tool(
        "awf_initialize_project_profile",
        {"project_path": project_path, "template": "generic"},
    )
    payload = _payload(result)
    rendered = _json_text(result)

    assert result.isError is True
    assert payload["error_code"] == "PROJECT_INIT_INVALID_PATH"
    assert payload["message"] == "project path does not exist"
    assert payload["detail"] == {"project_path": str(expected_project_path)}
    assert leaked_detail not in rendered


@pytest.mark.unit
async def test_initialize_project_profile_path_value_error_returns_structured_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    project_path = "bad\0path"
    expected_project_path = tmp_path / project_path

    monkeypatch.chdir(tmp_path)
    mcp = build_mcp_server(service=MagicMock(), settings=_settings(tmp_path))

    result = await mcp.call_tool(
        "awf_initialize_project_profile",
        {"project_path": project_path, "template": "generic"},
    )
    payload = _payload(result)

    assert result.isError is True
    assert payload["error_code"] == "PROJECT_INIT_INVALID_PATH"
    assert payload["message"] == "project path does not exist"
    assert payload["detail"] == {"project_path": str(expected_project_path)}


@pytest.mark.unit
async def test_initialize_project_profile_preview_failure_does_not_surface_exception_text(
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from awf.mcp import setup_tools

    project = tmp_path / "repo"
    project.mkdir()
    leaked_detail = "/srv/awf/internal/config.yml traceback frame"

    def fail_preview(
        _path: Path,
        *,
        template: str,
        include_smoke_request: bool,
    ) -> Any:
        _ = (template, include_smoke_request)
        raise RuntimeError(leaked_detail)

    monkeypatch.setattr(setup_tools, "preview_project_onboarding", fail_preview)
    caplog.set_level(logging.ERROR, logger="awf.mcp.setup_tools")
    mcp = build_mcp_server(service=MagicMock(), settings=_settings(tmp_path))

    result = await mcp.call_tool(
        "awf_initialize_project_profile",
        {"project_path": str(project), "template": "generic"},
    )
    payload = _payload(result)
    rendered = _json_text(result)

    assert result.isError is True
    assert payload["error_code"] == "PROJECT_INIT_FAILED"
    assert payload["message"] == "could not build onboarding preview"
    assert payload["detail"] == {
        "project_path": str(project.resolve()),
        "template": "generic",
    }
    assert leaked_detail not in rendered

    records = [
        record
        for record in caplog.records
        if record.name == "awf.mcp.setup_tools"
        and record.message == "could not build onboarding preview for MCP project initialization"
    ]
    assert len(records) == 1
    assert records[0].exc_info is not None
    assert records[0].project_path == str(project.resolve())
    assert records[0].template == "generic"


@pytest.mark.unit
async def test_initialize_project_profile_value_error_preview_failure_does_not_surface_exception_text(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from awf.mcp import setup_tools

    project = tmp_path / "repo"
    project.mkdir()
    leaked_detail = "/srv/awf/internal/template.yml unsupported onboarding template"

    def fail_preview(
        _path: Path,
        *,
        template: str,
        include_smoke_request: bool,
    ) -> Any:
        _ = (template, include_smoke_request)
        raise ValueError(leaked_detail)

    monkeypatch.setattr(setup_tools, "preview_project_onboarding", fail_preview)
    mcp = build_mcp_server(service=MagicMock(), settings=_settings(tmp_path))

    result = await mcp.call_tool(
        "awf_initialize_project_profile",
        {"project_path": str(project), "template": "generic"},
    )
    payload = _payload(result)
    rendered = _json_text(result)

    assert result.isError is True
    assert payload["error_code"] == "PROJECT_INIT_FAILED"
    assert payload["message"] == "could not build onboarding preview"
    assert payload["detail"] == {
        "project_path": str(project.resolve()),
        "template": "generic",
    }
    assert leaked_detail not in rendered


@pytest.mark.unit
async def test_initialize_project_profile_payload_assembly_failure_is_structured(
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from awf.mcp import setup_tools

    project = tmp_path / "repo"
    project.mkdir()
    leaked_detail = "/srv/awf/internal/onboarding-preview contains secret refs"
    preview = SimpleNamespace(
        path=project,
        draft=SimpleNamespace(template="generic"),
        to_dict=lambda: {
            "path": str(project),
            "inspection": {"detected_template": "generic"},
            "draft": {"template": "generic", "yaml": "name: generic\n"},
            "diagnostics": {},
        },
    )

    def fail_payload_assembly(**_kwargs: Any) -> dict[str, Any]:
        raise AttributeError(leaked_detail)

    monkeypatch.setattr(setup_tools, "preview_project_onboarding", lambda *_a, **_k: preview)
    monkeypatch.setattr(
        setup_tools,
        "_init_project_onboarding_payload",
        fail_payload_assembly,
    )
    caplog.set_level(logging.ERROR, logger="awf.mcp.setup_tools")
    mcp = build_mcp_server(service=MagicMock(), settings=_settings(tmp_path))

    result = await mcp.call_tool(
        "awf_initialize_project_profile",
        {"project_path": str(project), "template": "generic"},
    )
    payload = _payload(result)
    rendered = _json_text(result)

    assert result.isError is True
    assert payload["error_code"] == "PROJECT_INIT_FAILED"
    assert payload["message"] == "could not build onboarding payload"
    assert payload["detail"] == {
        "project_path": str(project.resolve()),
        "mode": "preview",
    }
    assert leaked_detail not in rendered

    records = [
        record
        for record in caplog.records
        if record.name == "awf.mcp.setup_tools"
        and record.message == "could not build project onboarding MCP payload"
    ]
    assert len(records) == 1
    assert records[0].exc_info is not None
    assert records[0].project_path == str(project.resolve())
    assert records[0].mode == "preview"


@pytest.mark.unit
async def test_initialize_project_profile_write_payload_failure_does_not_leave_profile_or_change_retry_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from awf.mcp import setup_tools

    project = tmp_path / "repo"
    project.mkdir()
    profile_path = project / ".awf" / "workspace.yml"
    leaked_detail = "/srv/awf/internal/onboarding-preview contains secret refs"
    preview = SimpleNamespace(
        path=project,
        draft=SimpleNamespace(template="generic", yaml="name: generic\n"),
        to_dict=lambda: {
            "path": str(project),
            "inspection": {"detected_template": "generic"},
            "draft": {"template": "generic", "yaml": "name: generic\n"},
            "diagnostics": {},
        },
    )

    def fail_payload_assembly(**_kwargs: Any) -> dict[str, Any]:
        raise AttributeError(leaked_detail)

    def fake_write(item: Any, *, force: bool) -> Path:
        assert item is preview
        if profile_path.exists() and not force:
            raise FileExistsError(17, "File exists", profile_path)
        profile_path.parent.mkdir(parents=True, exist_ok=True)
        profile_path.write_text(item.draft.yaml, encoding="utf-8")
        return profile_path

    monkeypatch.setattr(setup_tools, "preview_project_onboarding", lambda *_a, **_k: preview)
    monkeypatch.setattr(setup_tools, "write_workspace_profile", fake_write)
    monkeypatch.setattr(
        setup_tools,
        "_init_project_onboarding_payload",
        fail_payload_assembly,
    )
    mcp = build_mcp_server(service=MagicMock(), settings=_settings(tmp_path))

    first_result = await mcp.call_tool(
        "awf_initialize_project_profile",
        {"project_path": str(project), "template": "generic", "write_profile": True},
    )
    retry_result = await mcp.call_tool(
        "awf_initialize_project_profile",
        {"project_path": str(project), "template": "generic", "write_profile": True},
    )
    first_payload = _payload(first_result)
    retry_payload = _payload(retry_result)
    rendered = _json_text(first_result) + _json_text(retry_result)

    assert first_result.isError is True
    assert retry_result.isError is True
    assert first_payload["error_code"] == "PROJECT_INIT_FAILED"
    assert retry_payload["error_code"] == "PROJECT_INIT_FAILED"
    assert first_payload["message"] == "could not build onboarding payload"
    assert retry_payload["message"] == "could not build onboarding payload"
    assert first_payload["detail"] == {
        "project_path": str(project.resolve()),
        "mode": "write",
    }
    assert retry_payload["detail"] == first_payload["detail"]
    assert not profile_path.exists()
    assert leaked_detail not in rendered


@pytest.mark.unit
async def test_initialize_project_profile_existing_profile_probe_failure_logs_probe_context(
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from awf.mcp import setup_tools

    project = tmp_path / "repo"
    project.mkdir()
    leaked_detail = "/srv/awf/internal/.awf/workspace.yml permission denied"

    def fail_existing_profile_probe(_repository: Path) -> Path | None:
        raise PermissionError(leaked_detail)

    monkeypatch.setattr(
        setup_tools,
        "_existing_project_profile_path",
        fail_existing_profile_probe,
    )
    caplog.set_level(logging.ERROR, logger="awf.mcp.setup_tools")
    mcp = build_mcp_server(service=MagicMock(), settings=_settings(tmp_path))

    result = await mcp.call_tool(
        "awf_initialize_project_profile",
        {"project_path": str(project), "template": "generic"},
    )
    payload = _payload(result)
    rendered = _json_text(result)

    assert result.isError is True
    assert payload["error_code"] == "PROJECT_INIT_FAILED"
    assert payload["message"] == "could not build onboarding preview"
    assert payload["detail"] == {
        "project_path": str(project.resolve()),
        "template": "generic",
    }
    assert leaked_detail not in rendered

    records = [
        record
        for record in caplog.records
        if record.name == "awf.mcp.setup_tools"
        and record.message
        == "could not probe existing project profile for MCP project initialization"
    ]
    assert len(records) == 1
    assert records[0].exc_info is not None
    assert records[0].project_path == str(project.resolve())
    assert records[0].template == "generic"
