"""Worker runtime wiring for the local AWF service."""

from __future__ import annotations

import socket
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncEngine

from awf.control.worker import ControlWorker, WorkerConfig
from awf.db.session import make_engine, make_session_factory
from awf.node.auth_mounts import ServiceAuthMountResolver
from awf.node.compose_manager import ComposeManager
from awf.node.git_manager import GitManager
from awf.node.provisioner import Provisioner, ProvisionerConfig
from awf.node.stack_launcher import ComposeStackLauncher
from awf.service.config import ServiceSettings


@dataclass(frozen=True)
class WorkerRuntime:
    engine: AsyncEngine
    worker: ControlWorker


def build_worker_runtime(settings: ServiceSettings) -> WorkerRuntime:
    """Construct the DB/session, git manager, provisioner, and worker."""

    engine = make_engine(settings.database_url)
    session_factory = make_session_factory(engine)
    work_dir = Path(settings.work_dir).expanduser().resolve()
    template = Path(__file__).resolve().parents[3] / "docker" / "compose" / "workspace.base.yml.j2"
    git = GitManager(work_dir / "git")
    compose = ComposeManager(work_dir=work_dir, template_path=template)
    auth_mount_resolver = ServiceAuthMountResolver(
        host_home=Path(settings.host_home).expanduser().resolve(),
        work_dir=work_dir,
    )
    stack_launcher = ComposeStackLauncher(
        compose=compose,
        agent_runtime_image=settings.agent_runtime_image,
        auth_mount_resolver=auth_mount_resolver,
    )
    provisioner = Provisioner(
        session_factory=session_factory,
        git=git,
        stack_launcher=stack_launcher,
        config=ProvisionerConfig(
            node_id=settings.node_id or socket.gethostname(),
            branch_prefix=settings.branch_prefix,
        ),
    )
    worker = ControlWorker(
        session_factory=session_factory,
        provisioner=provisioner,
        config=WorkerConfig(
            poll_interval_seconds=settings.worker_poll_interval_seconds,
            max_concurrent_provisions=settings.worker_max_concurrent_provisions,
        ),
    )
    return WorkerRuntime(engine=engine, worker=worker)


async def run_worker(settings: ServiceSettings, *, once: bool = False) -> None:
    """Run the control worker, disposing the DB engine on exit."""

    runtime = build_worker_runtime(settings)
    try:
        if once:
            await runtime.worker.run_once()
            return
        await runtime.worker.run_forever()
    finally:
        await runtime.engine.dispose()
