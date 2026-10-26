"""Real TCP/PG acknowledgment ordering; synthetic host/provider, no worker launch."""

import asyncio
import socket

import pytest
import uvicorn
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.api.app import configure_database
from awf.db.models import QueueDecision, ResourceReservation, Task, TaskAttempt, Workspace
from tests.unit.test_hosted_runtime_authority import authority as authority
from tests.unit.test_hosted_runtime_review_create import (
    create_application as create_application,
)
from tests.unit.test_hosted_runtime_review_create import (
    readable_view,
)

_ROWS = (Workspace, Task, TaskAttempt, ResourceReservation, QueueDecision)
_PROBE = "durable_create_request"


@pytest.fixture
async def tcp_create(create_application, authority):
    app, headers = create_application
    _, _, factory = authority
    # Only route sessions participate in the barrier; authority reads retain
    # their ordinary factory and transaction semantics.
    request_factory = async_sessionmaker(
        **{**factory.kw, "info": {**factory.kw.get("info", {}), _PROBE: True}}
    )
    configure_database(app, request_factory)
    response_started = asyncio.Event()

    async def observed_app(scope, receive, send):
        async def observed_send(message):
            await send(message)
            if (
                scope["type"] == "http"
                and scope["method"] == "POST"
                and scope["path"] == "/v1/workspaces"
                and message["type"] == "http.response.start"
            ):
                response_started.set()

        await app(scope, receive, observed_send)

    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    server = uvicorn.Server(
        uvicorn.Config(observed_app, lifespan="off", access_log=False, log_level="error", ws="none")
    )
    serving = asyncio.create_task(server.serve(sockets=[listener]))
    try:
        async with asyncio.timeout(15):
            while not server.started:
                if serving.done():
                    await serving
                await asyncio.sleep(0.01)
        async with AsyncClient(
            base_url=f"http://127.0.0.1:{listener.getsockname()[1]}",
            headers=headers,
            trust_env=False,
            timeout=15,
        ) as client:
            yield client, response_started
    finally:
        server.should_exit = True
        await asyncio.wait_for(serving, 15)
        listener.close()


async def counts(factory):
    async with factory() as session:
        return [await session.scalar(select(func.count()).select_from(model)) for model in _ROWS]


def commit_barrier(monkeypatch, *, fail=False):
    original = AsyncSession.commit
    entered, release, finished = asyncio.Event(), asyncio.Event(), asyncio.Event()
    used = False

    async def commit(session):
        nonlocal used
        if session.info.get(_PROBE) and not used:
            used = True
            entered.set()
            try:
                await release.wait()
                if fail:
                    raise SQLAlchemyError("synthetic create commit failure")
                await original(session)
            finally:
                finished.set()
        else:
            await original(session)

    monkeypatch.setattr(AsyncSession, "commit", commit)
    return entered, release, finished


async def test_tcp_create_ack_waits_for_commit_and_is_durably_replayable(
    authority, tcp_create, monkeypatch
):
    runtime, value, factory = authority
    client, response_started = tcp_create
    entered, release, finished = commit_barrier(monkeypatch)
    request = asyncio.create_task(
        client.post("/v1/workspaces", json=value, headers={"Idempotency-Key": "durable"})
    )
    try:
        await asyncio.wait_for(entered.wait(), 15)
        assert await counts(factory) == [0] * len(_ROWS)
        assert not readable_view(runtime).exists()
        assert not response_started.is_set(), "HTTP acknowledgment escaped before commit"
    finally:
        release.set()
        response = await asyncio.wait_for(request, 15)
        await asyncio.wait_for(finished.wait(), 15)

    assert response.status_code == 202, response.text
    workspace_id = value["hosted"]["workspace_id"]
    assert response.json()["workspace_id"] == workspace_id
    assert await counts(factory) == [1] * len(_ROWS)
    async with factory() as session:
        row = await session.get(Workspace, workspace_id)
        assert row is not None and row.status == "requested"
    replay = await client.post("/v1/workspaces", json=value, headers={"Idempotency-Key": "durable"})
    assert replay.status_code == 202 and replay.json() == response.json()
    assert await counts(factory) == [1] * len(_ROWS)
    assert not readable_view(runtime).exists()


async def test_tcp_failed_commit_is_not_accepted_and_same_key_can_retry(
    authority, tcp_create, monkeypatch
):
    runtime, value, factory = authority
    client, _ = tcp_create
    entered, release, finished = commit_barrier(monkeypatch, fail=True)
    request = asyncio.create_task(
        client.post("/v1/workspaces", json=value, headers={"Idempotency-Key": "retry"})
    )
    try:
        await asyncio.wait_for(entered.wait(), 15)
        assert await counts(factory) == [0] * len(_ROWS)
    finally:
        release.set()
        response = await asyncio.wait_for(request, 15)
        await asyncio.wait_for(finished.wait(), 15)

    assert await counts(factory) == [0] * len(_ROWS)
    assert not readable_view(runtime).exists()
    retry = await client.post("/v1/workspaces", json=value, headers={"Idempotency-Key": "retry"})
    replay = await client.post("/v1/workspaces", json=value, headers={"Idempotency-Key": "retry"})
    assert [response.status_code, retry.status_code, replay.status_code] == [500, 202, 202]
    assert retry.json()["workspace_id"] == value["hosted"]["workspace_id"]
    assert replay.json() == retry.json()
    assert await counts(factory) == [1] * len(_ROWS)
    assert not readable_view(runtime).exists()
