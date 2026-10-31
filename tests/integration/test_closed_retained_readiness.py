"""Closed retained acceptance with real HTTP/materials/restricted PostgreSQL."""

import os
import sys
import tempfile
from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from awf.api.app import configure_database, create_app
from awf.db.models import Operation, Task, TaskAttempt, Workspace
from awf.db.session import make_session_factory
from awf.hosted_node.host_linux import LinuxObservation
from awf.hosted_node.host_runtime import HostRuntime, PhysicalFenceEvidence
from awf.hosted_node.identity import boot
from awf.hosted_node.material_store import MaterialStore
from awf.hosted_node.receiver import Receiver
from tests.integration.test_closed_host_database import runtime_database as runtime_database
from tests.integration.test_closed_host_runtime import ack
from tests.integration.test_closed_host_runtime import composed as composed
from tests.integration.test_host_boot_evidence import evidence
from tests.integration.test_node_idle_gate import PREFIX
from tests.integration.test_retained_work_acceptance import fresh_bindings
from tests.unit.test_hosted_runtime_contract import hosted_create


@pytest.fixture
async def closed_retained(composed, runtime_database, tmp_path, monkeypatch):
    client, old, prior_runtime, state = composed
    assert (await ack(client)).status_code == 200

    def sample(_self, *, require_quiescent=True):
        if require_quiescent and not state["linux"].execution_quiescent:
            raise ValueError("HOST_TASKS_ACTIVE")
        return state["linux"]

    monkeypatch.setattr(LinuxObservation, "sample", sample)
    if sys.platform != "linux":
        # Match the existing portable material fixture; Linux uses real tmpfs.
        def portable_root(self, root):
            self.root, self.closed = root, False
            fd = self._open_root()
            try:
                info = os.fstat(fd)
                self._device_inode = (info.st_dev, info.st_ino)
            finally:
                os.close(fd)

        monkeypatch.setattr(MaterialStore, "__init__", portable_root)
    with tempfile.TemporaryDirectory(
        dir="/dev/shm" if sys.platform == "linux" else tmp_path
    ) as directory:
        old.config = old.config.model_copy(update={"material_root": Path(directory)})
        old.store = MaterialStore(Path(directory))
        values = {}
        human_since = datetime.now(UTC) - timedelta(hours=1)
        for kind in ("queued", "monitor", "human", "unknown"):
            value = deepcopy(hosted_create())
            workspace_id = "ws_closed_" + kind
            value["hosted"]["workspace_id"] = workspace_id
            for binding in value["hosted"]["credentials"].values():
                binding["workspace_id"] = workspace_id
            value["hosted"]["credentials"] = await fresh_bindings(
                (client, SimpleNamespace(receiver=old), old.factory, None, value, tmp_path),
                prefix=f"old-{kind}-",
            )
            values[kind] = value
            status = (
                "requested"
                if kind == "queued"
                else "running"
                if kind == "unknown"
                else "monitoring_pr"
            )
            async with old.factory.begin() as session:
                session.add(
                    Workspace(
                        id=workspace_id,
                        node_id=old.identity.owner,
                        status=status,
                        repo_url="https://example.test/repo.git",
                        branch_base="main",
                        task_title=kind,
                        task_prompt="Synthetic, never execute",
                        agent="codex",
                        task_policy={"hosted_node": value["hosted"]},
                        pr_url="https://example.test/repo/pull/1"
                        if kind in {"monitor", "human"}
                        else None,
                        pr_number=1 if kind in {"monitor", "human"} else None,
                        awaiting_human_since=human_since if kind == "human" else None,
                        awaiting_human_reason="Existing human decision"
                        if kind == "human"
                        else None,
                    )
                )
                session.add(
                    Task(
                        id=kind,
                        repo_url="https://example.test/repo.git",
                        base_branch="main",
                        title=kind,
                        prompt="Synthetic",
                    )
                )
                await session.flush()
                session.add(
                    TaskAttempt(
                        id=kind,
                        task_id=kind,
                        workspace_id=workspace_id,
                        attempt_number=1,
                        agent="codex",
                        status=status,
                        title=kind,
                        repo_url="https://example.test/repo.git",
                        base_branch="main",
                    )
                )
                if kind == "unknown":
                    session.add(
                        Operation(
                            id="unknown-effect",
                            workspace_id=workspace_id,
                            type="run",
                            status="running",
                            payload={"external_submission": "unknown"},
                        )
                    )
        assert (
            await client.post(PREFIX + "/maintenance/begin", json={"generation": 7})
        ).status_code == 200
        config = old.config.model_copy(
            update={"incarnation": "8063738394745155542", "recovery_generation": 3}
        )
        factory = make_session_factory(runtime_database[3])
        current = await Receiver.start(config, factory)
        state["linux"] = replace(state["linux"], boot_id=str(uuid4()), workspaces_empty=False)
        runtime = HostRuntime(config, factory, current.identity, prior_runtime.docker)
        runtime.baseline = state["linux"]
        proofs = []

        async def fenced(previous, identity, receipt):
            proofs.append((previous, identity, receipt))
            return PhysicalFenceEvidence(
                previous, identity, receipt, datetime.now(UTC) + timedelta(seconds=20)
            )

        runtime.verify_physical_fence = fenced
        current.lifecycle.observe_host = runtime.observe
        current.lifecycle.observe_boot = runtime.observe_boot
        current.lifecycle.verify_recovery = runtime.recover
        app = create_app(use_lifespan=False)
        configure_database(app, factory)
        app.state.hosted_receiver = current
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://core.test",
            headers={"Authorization": "Bearer synthetic-controller", **current.identity.headers()},
        ) as joined:
            try:
                for kind in ("queued", "monitor", "human"):
                    await fresh_bindings(
                        (
                            joined,
                            SimpleNamespace(receiver=current),
                            factory,
                            old.identity,
                            values[kind],
                            tmp_path,
                        ),
                        prefix=f"fresh-{kind}-",
                    )
                yield SimpleNamespace(
                    client=joined,
                    receiver=current,
                    runtime=runtime,
                    factory=factory,
                    state=state,
                    previous=old.identity,
                    proofs=proofs,
                    human_since=human_since,
                )
            finally:
                await current.close()
                old.store.close()


async def committed(case):
    response = await ack(case.client, "trusted-retained-receipt")
    assert response.status_code == 200, response.text
    result = response.json()
    acceptance = result["recovery_acceptance"]
    assert acceptance["previous_identity"] == case.previous.model_dump()
    assert (
        acceptance["queued_workspaces"],
        acceptance["monitor_workspaces"],
        acceptance["attention_workspaces"],
    ) == (1, 2, 1)
    assert len(case.proofs) == 2
    return result


async def test_closed_retained_acceptance_reports_health_without_idle_or_runtime(closed_retained):
    case = closed_retained
    before = (await case.client.get(PREFIX + "/readiness")).json()
    assert not before["node_ready"] and not before["runtime_ready"] and before["admission_closed"]
    result = await committed(case)
    assert result["node_ready"] and result["expires_at"] is not None
    assert (
        not result["quiescent"] and not result["database_quiescent"] and not result["runtime_ready"]
    )
    assert result["admission_closed"] and result["maintenance_generation"] == 7
    assert result["idle_since"] is None
    readback = (await case.client.get(PREFIX + "/readiness")).json()
    assert (
        readback["node_ready"] and readback["recovery_acceptance"] == result["recovery_acceptance"]
    )
    replay = (await ack(case.client, "trusted-retained-receipt")).json()
    assert replay["recovery_acceptance"] == result["recovery_acceptance"] and len(case.proofs) == 2
    async with case.factory() as session:
        unknown = await session.get(Workspace, "ws_closed_unknown")
        human = await session.get(Workspace, "ws_closed_human")
        operation = await session.get(Operation, "unknown-effect")
        assert unknown.status == operation.status == "running"
        assert unknown.task_policy["hosted_recovery"]["disposition"] == "attention"
        assert human.awaiting_human_since == case.human_since
        assert human.awaiting_human_reason == "Existing human decision"
    with pytest.raises(DBAPIError, match="RECOVERY_ATTENTION_REQUIRED"):
        async with case.factory.begin() as session:
            await session.execute(
                text(
                    "UPDATE workspaces SET execution_claimed_by='worker' WHERE id='ws_closed_unknown'"
                )
            )


async def test_post_ack_closed_boot_evidence_preserves_exact_acceptance(closed_retained):
    case = closed_retained
    accepted = await committed(case)
    response = await evidence(case.client)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["identity"] == case.receiver.identity.model_dump()
    assert body["previous_identity"] == case.previous.model_dump()
    assert body["kernel_boot_id"] == case.state["linux"].boot_id
    assert body["disk_ref"] == "disk" and body["database_ref"] == "db"
    assert body["recovery_acknowledged"] and body["admission_closed"] and not body["runtime_ready"]
    assert datetime.now(UTC) < datetime.fromisoformat(body["expires_at"])
    current = (await case.client.get(PREFIX + "/readiness")).json()
    assert current["recovery_acceptance"] == accepted["recovery_acceptance"]
    assert current["maintenance_generation"] == 7 and not current["database_quiescent"]


@pytest.mark.parametrize("missing", ["hold", "acknowledgement", "acceptance"])
async def test_retained_post_ack_boot_requires_held_committed_acceptance(closed_retained, missing):
    case = closed_retained
    await committed(case)
    if missing == "hold":
        response = await case.client.post(PREFIX + "/maintenance/release", json={"generation": 7})
        assert response.status_code == 200 and response.json()["maintenance_generation"] is None
    else:
        column = "recovery_key" if missing == "acknowledgement" else "recovery_acceptance"
        async with case.factory.begin() as session:
            await session.execute(text(f"UPDATE hosted_node_lifecycle SET {column}=NULL"))
    assert (await evidence(case.client)).status_code == 503


@pytest.mark.parametrize(
    "cause", ["container", "execution", "disk", "boot", "database", "role", "stale", "drain"]
)
async def test_post_ack_retained_rows_do_not_mask_invalid_closed_host(closed_retained, cause):
    case = closed_retained
    await committed(case)
    if cause == "container":
        case.state["containers"] = [{"Id": "still-active-producer"}]
    elif cause in {"execution", "disk", "boot"}:
        field, value = {
            "execution": ("execution_quiescent", False),
            "disk": ("device", 999),
            "boot": ("boot_id", str(uuid4())),
        }[cause]
        case.state["linux"] = replace(case.state["linux"], **{field: value})
    elif cause in {"database", "role"}:
        field = "database_name" if cause == "database" else "database_role"
        case.runtime.config = case.runtime.config.model_copy(
            update={"host": case.runtime.config.host.model_copy(update={field: "wrong"})}
        )
    elif cause == "stale":
        await boot(case.receiver.config, make_session_factory(case.factory.kw["bind"]))
    else:
        result = await case.client.post(PREFIX + "/drain", json={"generation": 8})
        # Logical work still prevents starting drain. Independently exercise
        # the boot/readiness guard against an already persisted drain state.
        assert result.status_code == 200 and result.json()["state"] == "ready"
        assert result.json()["drain_generation"] is None
        async with case.factory.begin() as session:
            await session.execute(
                text("UPDATE hosted_node_lifecycle SET phase='draining', drain_generation=8")
            )
    response = await evidence(case.client)
    assert response.status_code == (403 if cause == "stale" else 503)
    readiness = await case.client.get(PREFIX + "/readiness")
    if cause == "stale":
        assert readiness.status_code == 403
    else:
        result = readiness.json()
        assert not result["node_ready"] and not result["runtime_ready"]
        assert result["admission_closed"] and result["maintenance_generation"] == 7
