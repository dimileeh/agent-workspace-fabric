"""Focused coverage for workspace CLI request construction."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
import typer

from awf.cli import workspace_commands
from awf.cli.common import OutputFormat
from awf.db.enums import TaskClass, TaskKind


@pytest.mark.unit
def test_workspace_create_builds_full_v1_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify workspace_create assembles the full v1 API payload with all optional fields."""
    captured: dict[str, object] = {}

    def _call(method: str, path: str, **kwargs: object) -> httpx.Response:
        captured["method"] = method
        captured["path"] = path
        captured.update(kwargs)
        return httpx.Response(202, json={"id": "ws"})

    monkeypatch.setattr(workspace_commands, "_call", _call)
    monkeypatch.setattr(workspace_commands, "_handle_response", lambda *_args, **_kwargs: None)

    workspace_commands.workspace_create(
        repo_url="git@example.com:repo/app.git",
        task_title="title",
        task_prompt="prompt",
        branch_base=None,
        task_kind=TaskKind.sync_release_pr.value,
        source_branch="release/source",
        agent="codex",
        model="gpt-5.5",
        effort="xhigh",
        task_class=TaskClass.refactor_task,
        priority=7,
        human_boost=3,
        out_of_scope_changes_json='{"mode":"warn"}',
        provider_recovery_json='{"enabled":true}',
        owned_paths=["src/awf"],
        external_id="task-1",
        cpu=2.0,
        memory="4g",
        steady_state_cpu_cores=1.0,
        steady_state_memory_gb=2.0,
        peak_cpu_cores=3.0,
        peak_memory_gb=6.0,
        disk_mb=1024,
        profile_ref="auto",
        test_commands=["pytest -q"],
        requires_database=True,
        auto_merge=False,
        initial_review_grace_period_seconds=15.0,
        provider_readiness_override=True,
        provider_readiness_override_reason="operator approved",
        companion_json=[
            '{"name":"backend","repo_url":"git@example.com:repo/api.git",'
            '"build_context":"services/api","depends_on":["docker"]}'
        ],
        companion_env_from=None,
        companion_env_exclude=None,
        idempotency_key="idem-1",
        api_token="token",
        base_url="http://api",
        fmt=OutputFormat.json,
    )

    assert captured["method"] == "POST"
    assert captured["path"] == "/v1/workspaces"
    assert captured["base_url"] == "http://api"
    assert captured["headers"] == {"Authorization": "Bearer token", "Idempotency-Key": "idem-1"}
    assert captured["json"] == {
        "repo": {
            "url": "git@example.com:repo/app.git",
            "base_branch": "main",
            "source_branch": "release/source",
        },
        "task": {
            "title": "title",
            "prompt": "prompt",
            "agent": "codex",
            "kind": "sync_release_pr",
            "auto_merge": False,
            "initial_review_grace_period_seconds": 15.0,
            "model": "gpt-5.5",
            "effort": "xhigh",
            "task_class": "refactor_task",
            "external_id": "task-1",
            "priority": 7,
            "human_boost": 3,
            "out_of_scope_changes": {"mode": "warn"},
            "provider_recovery": {"enabled": True},
            "owned_paths": ["src/awf"],
        },
        "workspace": {"profile_ref": "aira", "profile": None},
        "validation": {"commands": ["pytest -q"], "requested_tier": 1},
        "resources": {
            "cpu": 2.0,
            "memory": "4g",
            "steady_state_cpu_cores": 1.0,
            "steady_state_memory_gb": 2.0,
            "peak_cpu_cores": 3.0,
            "peak_memory_gb": 6.0,
            "disk_mb": 1024,
        },
        "preflight": {
            "provider_readiness_override": True,
            "provider_readiness_override_reason": "operator approved",
        },
        "companions": [
            {
                "name": "backend",
                "repo_url": "git@example.com:repo/api.git",
                "build_context": "services/api",
                "depends_on": ["docker"],
            }
        ],
    }


@pytest.mark.unit
@pytest.mark.unit
def test_workspace_create_builds_minimal_development_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify workspace_create produces a minimal payload when only required fields are provided."""
    captured: dict[str, object] = {}

    def _call(method: str, path: str, **kwargs: object) -> httpx.Response:
        captured["method"] = method
        captured["path"] = path
        captured.update(kwargs)
        return httpx.Response(202, json={"id": "ws"})

    monkeypatch.setattr(workspace_commands, "_call", _call)
    monkeypatch.setattr(workspace_commands, "_handle_response", lambda *_args, **_kwargs: None)

    workspace_commands.workspace_create(
        repo_url="git@example.com:repo/app.git",
        task_title="title",
        task_prompt="prompt",
        branch_base=None,
        task_kind=TaskKind.feature_branch_pr.value,
        source_branch=None,
        agent="codex",
        model=None,
        effort=None,
        task_class=None,
        priority=None,
        human_boost=None,
        out_of_scope_changes_json=None,
        provider_recovery_json=None,
        owned_paths=None,
        external_id=None,
        cpu=None,
        memory=None,
        steady_state_cpu_cores=None,
        steady_state_memory_gb=None,
        peak_cpu_cores=None,
        peak_memory_gb=None,
        disk_mb=None,
        profile_ref="auto",
        test_commands=[],
        requires_database=False,
        auto_merge=True,
        initial_review_grace_period_seconds=None,
        provider_readiness_override=False,
        provider_readiness_override_reason=None,
        companion_json=None,
        companion_env_from=None,
        companion_env_exclude=None,
        idempotency_key=None,
        api_token=None,
        base_url=None,
        fmt=OutputFormat.json,
    )

    assert captured["method"] == "POST"
    assert captured["path"] == "/v1/workspaces"
    assert captured["json"] == {
        "repo": {
            "url": "git@example.com:repo/app.git",
            "base_branch": "development",
        },
        "task": {
            "title": "title",
            "prompt": "prompt",
            "agent": "codex",
            "kind": "feature_branch_pr",
            "auto_merge": True,
            "initial_review_grace_period_seconds": None,
        },
        "workspace": {"profile_ref": "auto", "profile": None},
        "validation": {"commands": [], "requested_tier": 1},
        "resources": {},
        "preflight": {
            "provider_readiness_override": False,
            "provider_readiness_override_reason": None,
        },
        "companions": [],
    }


@pytest.mark.unit
@pytest.mark.unit
def test_workspace_create_merges_env_from_into_companion(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Verify --companion-env-from merges .env entries into the companion environment, with payload values winning on conflict."""
    captured: dict[str, object] = {}

    def _call(method: str, path: str, **kwargs: object) -> httpx.Response:
        captured["method"] = method
        captured["path"] = path
        captured.update(kwargs)
        return httpx.Response(202, json={"id": "ws"})

    monkeypatch.setattr(workspace_commands, "_call", _call)
    monkeypatch.setattr(workspace_commands, "_handle_response", lambda *_args, **_kwargs: None)

    env_file = tmp_path / ".env"
    env_file.write_text("DB_PORT=5432\nAPP_DEBUG=true\n")

    workspace_commands.workspace_create(
        repo_url="git@example.com:repo/app.git",
        task_title="title",
        task_prompt="prompt",
        branch_base=None,
        task_kind=TaskKind.feature_branch_pr.value,
        source_branch=None,
        agent="codex",
        model=None,
        effort=None,
        task_class=None,
        priority=None,
        human_boost=None,
        out_of_scope_changes_json=None,
        provider_recovery_json=None,
        owned_paths=None,
        external_id=None,
        cpu=None,
        memory=None,
        steady_state_cpu_cores=None,
        steady_state_memory_gb=None,
        peak_cpu_cores=None,
        peak_memory_gb=None,
        disk_mb=None,
        profile_ref="auto",
        test_commands=[],
        requires_database=False,
        auto_merge=True,
        initial_review_grace_period_seconds=None,
        provider_readiness_override=False,
        provider_readiness_override_reason=None,
        companion_json=[
            '{"name":"svc","repo_url":"git@x:svc.git","environment":{"DB_HOST":"postgres:5432"}}'
        ],
        companion_env_from=[f"svc={env_file}"],
        companion_env_exclude=None,
        idempotency_key=None,
        api_token=None,
        base_url=None,
        fmt=OutputFormat.json,
    )

    body = captured["json"]
    companions = body["companions"]
    assert len(companions) == 1
    env = companions[0]["environment"]
    assert env["DB_HOST"] == "postgres:5432"  # payload wins
    assert env["DB_PORT"] == "5432"  # from file
    assert env["APP_DEBUG"] == "true"  # from file


@pytest.mark.unit
@pytest.mark.unit
def test_workspace_create_env_exclude_drops_keys(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Verify --companion-env-exclude drops specified keys from the merged companion environment."""
    captured: dict[str, object] = {}

    def _call(method: str, path: str, **kwargs: object) -> httpx.Response:
        captured["method"] = method
        captured["path"] = path
        captured.update(kwargs)
        return httpx.Response(202, json={"id": "ws"})

    monkeypatch.setattr(workspace_commands, "_call", _call)
    monkeypatch.setattr(workspace_commands, "_handle_response", lambda *_args, **_kwargs: None)

    env_file = tmp_path / ".env"
    env_file.write_text("KEEP=this\nDROP=that\n")

    workspace_commands.workspace_create(
        repo_url="git@example.com:repo/app.git",
        task_title="title",
        task_prompt="prompt",
        branch_base=None,
        task_kind=TaskKind.feature_branch_pr.value,
        source_branch=None,
        agent="codex",
        model=None,
        effort=None,
        task_class=None,
        priority=None,
        human_boost=None,
        out_of_scope_changes_json=None,
        provider_recovery_json=None,
        owned_paths=None,
        external_id=None,
        cpu=None,
        memory=None,
        steady_state_cpu_cores=None,
        steady_state_memory_gb=None,
        peak_cpu_cores=None,
        peak_memory_gb=None,
        disk_mb=None,
        profile_ref="auto",
        test_commands=[],
        requires_database=False,
        auto_merge=True,
        initial_review_grace_period_seconds=None,
        provider_readiness_override=False,
        provider_readiness_override_reason=None,
        companion_json=['{"name":"svc","repo_url":"git@x:svc.git"}'],
        companion_env_from=[f"svc={env_file}"],
        companion_env_exclude=["svc=DROP"],
        idempotency_key=None,
        api_token=None,
        base_url=None,
        fmt=OutputFormat.json,
    )

    body = captured["json"]
    env = body["companions"][0]["environment"]
    assert "KEEP" in env
    assert "DROP" not in env


@pytest.mark.unit
@pytest.mark.unit
def test_workspace_create_env_from_missing_companion_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify --companion-env-from referencing a nonexistent companion name exits with code 2."""
    monkeypatch.setattr(
        workspace_commands,
        "_call",
        lambda *_args, **_kwargs: httpx.Response(202, json={"id": "ws"}),
    )
    monkeypatch.setattr(workspace_commands, "_handle_response", lambda *_args, **_kwargs: None)

    with pytest.raises(typer.Exit) as raised:
        workspace_commands.workspace_create(
            repo_url="git@example.com:repo/app.git",
            task_title="title",
            task_prompt="prompt",
            branch_base=None,
            task_kind=TaskKind.feature_branch_pr.value,
            source_branch=None,
            agent="codex",
            model=None,
            effort=None,
            task_class=None,
            priority=None,
            human_boost=None,
            out_of_scope_changes_json=None,
            provider_recovery_json=None,
            owned_paths=None,
            external_id=None,
            cpu=None,
            memory=None,
            steady_state_cpu_cores=None,
            steady_state_memory_gb=None,
            peak_cpu_cores=None,
            peak_memory_gb=None,
            disk_mb=None,
            profile_ref="auto",
            test_commands=[],
            requires_database=False,
            auto_merge=True,
            initial_review_grace_period_seconds=None,
            provider_readiness_override=False,
            provider_readiness_override_reason=None,
            companion_json=['{"name":"svc","repo_url":"git@x:svc.git"}'],
            companion_env_from=["nonexistent=/tmp/.env"],
            companion_env_exclude=None,
            idempotency_key=None,
            api_token=None,
            base_url=None,
            fmt=OutputFormat.json,
        )
    assert raised.value.exit_code == 2


@pytest.mark.unit
@pytest.mark.unit
def test_workspace_create_rejects_non_object_companion_json(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify workspace_create rejects a companion JSON that is not a JSON object (e.g. an array)."""
    monkeypatch.setattr(workspace_commands, "_call", lambda *_args, **_kwargs: None)

    with pytest.raises(typer.Exit) as raised:
        workspace_commands.workspace_create(
            repo_url="git@example.com:repo/app.git",
            task_title="title",
            task_prompt="prompt",
            branch_base=None,
            task_kind=TaskKind.feature_branch_pr.value,
            source_branch=None,
            agent="codex",
            model=None,
            effort=None,
            task_class=None,
            priority=None,
            human_boost=None,
            out_of_scope_changes_json=None,
            provider_recovery_json=None,
            owned_paths=None,
            external_id=None,
            cpu=None,
            memory=None,
            steady_state_cpu_cores=None,
            steady_state_memory_gb=None,
            peak_cpu_cores=None,
            peak_memory_gb=None,
            disk_mb=None,
            profile_ref="auto",
            test_commands=[],
            requires_database=False,
            auto_merge=True,
            initial_review_grace_period_seconds=None,
            provider_readiness_override=False,
            provider_readiness_override_reason=None,
            companion_json=["[]"],
            companion_env_from=None,
            companion_env_exclude=None,
            idempotency_key=None,
            api_token=None,
            base_url=None,
            fmt=OutputFormat.json,
        )

    assert raised.value.exit_code == 2


@pytest.mark.unit
@pytest.mark.parametrize(
    "kwargs",
    [
        {"repo": None, "pr_number": None, "pr_url": None},
        {"repo": "owner/repo", "pr_number": None, "pr_url": "https://github.com/owner/repo/pull/1"},
        {"repo": None, "pr_number": 1, "pr_url": "https://github.com/owner/repo/pull/1"},
    ],
)
def test_workspace_adopt_pr_requires_exactly_one_selector(
    kwargs: dict[str, object],
) -> None:
    """Verify workspace_adopt_pr raises BadParameter when zero or multiple selectors are provided."""
    with pytest.raises(typer.BadParameter, match="exactly one selector"):
        workspace_commands.workspace_adopt_pr(
            repo=kwargs["repo"],  # type: ignore[arg-type]
            pr_number=kwargs["pr_number"],  # type: ignore[arg-type]
            pr_url=kwargs["pr_url"],  # type: ignore[arg-type]
            agent="codex",
            model=None,
            effort=None,
            owned_paths=None,
            profile_ref="auto",
            auto_merge=True,
            initial_review_grace_period_seconds=None,
            task_title=None,
            task_prompt=None,
            reason=None,
            api_token=None,
            base_url=None,
            fmt=OutputFormat.json,
        )
