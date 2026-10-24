"""The API host lease owns one normal worker using the same accepted boot."""

import asyncio
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy.exc import SQLAlchemyError

from awf.db.models import WorkerHeartbeat
from awf.hosted_node.identity import MaterialDeniedError
from awf.hosted_node.receiver import Receiver
from awf.hosted_node.runtime_authority import SESSION_KEY, WorkspaceAuthority
from awf.hosted_node.runtime_capacity import validate_capacity
from awf.hosted_node.runtime_consumers import WorkspaceConsumers
from awf.hosted_node.schemas import Identity
from awf.service.config import ServiceSettings
from awf.service.local_capacity import detect_local_capacity
from awf.service.worker import build_worker_runtime


class HostedWorker:
    def __init__(self, receiver: Receiver, settings: ServiceSettings):
        host = receiver.config.host
        if host is None or Path(settings.work_dir) != host.workspaces_root:
            raise MaterialDeniedError("HOSTED_WORK_ROOT_MISMATCH", 503)
        if (
            not settings.docker_host.startswith("unix:///")
            or settings.docker_host == f"unix://{host.docker_socket}"
        ):
            raise MaterialDeniedError("EXECUTION_DOCKER_ENDPOINT_REQUIRED", 503)
        self.settings = settings
        self.capacity_environment = {
            "PATH": os.environ.get("PATH", os.defpath),
            "HOME": str(Path(settings.work_dir) / "hosted-empty-home"),
        }
        validate_capacity(
            settings,
            receiver.config.runtime_capacity,
            detect_local_capacity(settings, environment=self.capacity_environment),
        )
        self.authority = WorkspaceAuthority(receiver)
        self.consumers = WorkspaceConsumers(self.authority, Path(settings.work_dir))
        self.consumers.base_environment["DOCKER_HOST"] = settings.docker_host
        self.runtime = build_worker_runtime(settings, hosted=self.consumers)
        self.task = asyncio.create_task(
            self.runtime.worker.run_forever(), name="hosted-core-worker"
        )

    async def is_ready(self, identity: Identity) -> bool:
        if self.task.done() or identity != self.authority.receiver.identity:
            return False
        try:
            await self.authority.admission()
            validate_capacity(
                self.settings,
                self.authority.receiver.config.runtime_capacity,
                await asyncio.to_thread(
                    detect_local_capacity, self.settings, environment=self.capacity_environment
                ),
            )
            async with self.authority.receiver.factory() as session:
                heartbeat = await session.get(WorkerHeartbeat, self.runtime.worker._worker_id)
                return bool(
                    heartbeat is not None
                    and heartbeat.node_id == identity.owner
                    and datetime.now(UTC) - timedelta(seconds=30)
                    <= heartbeat.last_heartbeat_at
                    <= datetime.now(UTC)
                )
        except (MaterialDeniedError, SQLAlchemyError, OSError, TimeoutError):
            return False

    async def admission(self) -> None:
        if not await self.is_ready(self.authority.receiver.identity):
            raise MaterialDeniedError("HOSTED_RUNTIME_UNAVAILABLE", 503)


@asynccontextmanager
async def start_runtime(
    receiver: Receiver, settings: ServiceSettings
) -> AsyncIterator[HostedWorker]:
    if not receiver.config.runtime_enabled or receiver.lifecycle is None:
        raise MaterialDeniedError("HOSTED_RUNTIME_DISABLED", 403)
    runtime = HostedWorker(receiver, settings)
    receiver.lifecycle.observe_runtime = runtime.is_ready
    receiver.factory.configure(
        info={**receiver.factory.kw.get("info", {}), SESSION_KEY: runtime.authority}
    )
    try:
        yield runtime
    finally:
        runtime.runtime.worker.request_stop()
        runtime.task.cancel()
        try:
            with suppress(asyncio.CancelledError):
                await runtime.task
            tasks = list(runtime.runtime.worker._execution_tasks.values())
            for task in tasks:
                task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
        finally:
            await runtime.runtime.engine.dispose()
