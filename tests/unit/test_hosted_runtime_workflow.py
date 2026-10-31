"""Canonical HTTP -> PG -> normal worker/checkout/validation/PR monitor.

Only external Docker/agent and forge effects are synthetic. Git uses a real
throwaway repository; no remote provider, paid agent, or live container is used.
"""

import asyncio
import subprocess
import sys
from dataclasses import replace

import pytest
import yaml
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

from awf.api.app import configure_database, create_app
from awf.common.commands import AsyncioSubprocessRunner, CommandResult
from awf.common.config import Settings, get_settings
from awf.db.models import Workspace
from awf.hosted_node.runtime_consumers import WorkspaceConsumers
from awf.service import provider_readiness, worker
from tests.conftest import _ok_workspace_admission_disk_check
from tests.unit.common.test_github_client_parts.test_github_client_part_001 import (
    _sample_pr_payload,
)
from tests.unit.node.test_provisioner_parts.test_provisioner_part_001 import (
    origin_repo as origin_repo,
)
from tests.unit.service.test_worker import _settings
from tests.unit.test_hosted_runtime_authority import authority as authority


@pytest.mark.parametrize("postgres_profile", [False, True])
async def test_canonical_create_drives_real_worker_to_pr_monitor_completion(
    authority, origin_repo, tmp_path, monkeypatch, postgres_profile
):
    authority, payload, factory = authority
    work = tmp_path / "work"
    url = payload["repo"]["url"]
    payload["repo"]["base_branch"] = "development"
    payload["task"]["auto_merge"] = False
    payload["workspace"] = {"profile_ref": None, "profile": {"name": "hosted-synthetic"}}
    if postgres_profile:
        payload["workspace"]["profile"].update(
            {
                "docker": {"mode": "none", "compose_files": [], "project_directory": "."},
                "runtime": {"environment": {"AWF_POSTGRES_PASSWORD": "synthetic-db-only"}},
                "secrets": [],
                "services": [
                    {
                        "name": "postgres",
                        "image": "pgvector/pgvector:pg18",
                        "environment": {"POSTGRES_PASSWORD": "${AWF_POSTGRES_PASSWORD}"},
                        "volumes": [["postgres_runtime", "/var/run/postgresql"]],
                        "healthcheck_cmd": "pg_isready -U postgres",
                    }
                ],
            }
        )
        profile = payload["workspace"]["profile"]
        (origin_repo / ".awf").mkdir()
        (origin_repo / ".awf/workspace.yml").write_text(yaml.safe_dump({"awf": profile}))
        subprocess.run(["git", "add", ".awf/workspace.yml"], cwd=origin_repo, check=True)
        subprocess.run(
            ["git", "commit", "-qm", "Synthetic ordinary repository profile"],
            cwd=origin_repo,
            check=True,
        )
        payload["workspace"] = {"profile_ref": "auto"}
    payload["validation"] = {"commands": ["python -m pytest -q"]}
    monkeypatch.setattr(
        provider_readiness,
        "_run_subprocess",
        lambda args, **_kwargs: subprocess.CompletedProcess(args, 0, "synthetic-cli", ""),
    )

    real_spawn = asyncio.create_subprocess_exec
    docker_commands = []
    rendered_manifests = []

    async def spawn(*args, **kwargs):
        if args[0] == "docker":
            docker_commands.append(args)
            if "up" in args:
                rendered_manifests.append(
                    (work / "compose/ws_cloud_stable/compose.yml").read_text()
                )
            return await real_spawn(sys.executable, "-c", "pass", **kwargs)
        args = tuple(str(origin_repo) if item == url else item for item in args)
        return await real_spawn(*args, **kwargs)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    real = AsyncioSubprocessRunner()
    calls = []

    class ExternalEffects:
        async def run(self, args, **kwargs):
            calls.append((list(args), kwargs.get("env")))
            if args[0] == "git":
                return await real.run(args, **kwargs)
            if args[:3] == ["gh", "pr", "create"]:
                return CommandResult(0, "https://github.com/example/repo/pull/42\n", "")
            if args[:3] == ["gh", "api", "graphql"]:
                return CommandResult(0, _sample_pr_payload(merged=True, closed=True), "")
            if args[0] == "docker":
                return CommandResult(0, "[]" if "ps" in args or "inspect" in args else "", "")
            raise AssertionError(f"unexpected external command: {args[0]}")

        async def run_streaming(self, args, **kwargs):
            if args[0] == "git":
                return await real.run_streaming(args, **kwargs)
            calls.append((list(args), kwargs.get("env")))
            assert args[:2] == ["docker", "compose"], args
            if "codex" in args:
                (work / "git/worktrees/ws_cloud_stable/implemented.txt").write_text(
                    "synthetic agent implemented task\n"
                )
            callback = kwargs.get("on_stdout")
            if callback:
                await callback("synthetic external effect completed\n")
            return CommandResult(0, "synthetic external effect completed\n", "")

    monkeypatch.setattr(worker, "AsyncioSubprocessRunner", ExternalEffects)
    engine = factory.kw["bind"]
    async with engine.connect() as connection:
        schema = await connection.scalar(text("select current_schema()"))
    database_url = engine.url.update_query_dict({"awf_search_path": schema}).render_as_string(
        hide_password=False
    )
    settings = replace(
        _settings(tmp_path),
        local_capacity_cpu_cores=12,
        local_capacity_memory_gb=32,
        worker_max_concurrent_provisions=2,
        worker_max_concurrent_executions=2,
        database_url=database_url,
        github_token=None,
        work_dir=str(work),
        auto_cleanup_orphans=False,
        terminal_workspace_gc_enabled=False,
        claude_base_gc_enabled=False,
    )
    consumers = WorkspaceConsumers(authority, work)
    runtime = worker.build_worker_runtime(settings, hosted=consumers)
    app = create_app(use_lifespan=False)
    configure_database(app, factory)
    app.state.hosted_receiver, app.state.hosted_runtime = authority.receiver, authority
    api_settings = Settings(
        _env_file=None, api_token="synthetic-operator", hosted_node=authority.receiver.config
    )
    app.dependency_overrides[get_settings] = lambda: api_settings
    app.state.workspace_admission_disk_check = _ok_workspace_admission_disk_check
    factory.configure(info={**factory.kw.get("info", {}), "awf_hosted_runtime": authority})
    headers = {
        "Authorization": "Bearer synthetic-controller",
        "Idempotency-Key": "normal-workflow",
        **authority.receiver.identity.headers(),
    }
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post("/v1/workspaces", json=payload, headers=headers)
            assert response.status_code == 202, response.text
            assert response.json()["workspace_id"] == "ws_cloud_stable"
            # The shared Postgres-test watchdog bounds the whole workflow;
            # HTTP/Git/DB progress is not required to complete within 20s.
            await runtime.worker.run_once()
            tasks = list(runtime.worker._execution_tasks.values())
            if tasks:
                await asyncio.gather(*tasks)
            async with factory() as session:
                workspace = await session.get(Workspace, "ws_cloud_stable")
                assert workspace is not None
                assert workspace.status == "completed", (
                    workspace.status,
                    workspace.failure_reason,
                    workspace.failure_message,
                )
                assert workspace.pr_number == 42
                assert workspace.pr_merge_sha == "mergecommit1234567890"
                assert workspace.node_id == authority.receiver.identity.owner
            status = await client.get(
                "/v1/workspaces/ws_cloud_stable",
                headers={**headers, "Authorization": "Bearer synthetic-operator"},
            )
            assert status.status_code == 200, status.text
            assert status.json()["status"] == "completed"
        assert len(rendered_manifests) == 1
        rendered = rendered_manifests[0]
        compose = yaml.safe_load(rendered)
        limits = [s["deploy"]["resources"]["limits"] for s in compose["services"].values()]
        assert sum(float(item["cpus"]) for item in limits) == 6
        assert sum(int(item["memory"]) for item in limits) == 16 * 1024**3
        assert all(service["cgroup_parent"] == "/test" for service in compose["services"].values())
        if postgres_profile:
            assert compose["services"]["postgres"]["image"] == "pgvector/pgvector:pg18"
            assert (
                "postgres_runtime:/var/run/postgresql" in compose["services"]["postgres"]["volumes"]
            )
            assert "privileged" not in compose["services"]["agent"]
            assert not any(
                "docker.sock" in mount for mount in compose["services"]["agent"]["volumes"]
            )
        assert any(
            ":/run/awf/secrets:ro" in mount for mount in compose["services"]["agent"]["volumes"]
        )
        assert "synthetic-agent-key" not in rendered and "synthetic-github-key" not in rendered
        assert any("codex" in args for args, _ in calls)
        assert any(args[:3] == ["gh", "pr", "create"] for args, _ in calls)
        assert any(args[:3] == ["gh", "api", "graphql"] for args, _ in calls)
        assert all(env["GH_TOKEN"] == "synthetic-github-key" for _, env in calls)
        assert docker_commands
    finally:
        for task in runtime.worker._execution_tasks.values():
            if not task.done():
                task.cancel()
        await runtime.engine.dispose()
