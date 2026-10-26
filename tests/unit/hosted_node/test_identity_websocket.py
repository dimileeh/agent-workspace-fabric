import asyncio

from awf.hosted_node.receiver import Receiver


async def websocket(app, headers, *, path="/v1/workspaces/ws/ws"):
    incoming, outgoing = asyncio.Queue(), asyncio.Queue()
    await incoming.put({"type": "websocket.connect"})
    scope = {
        "type": "websocket",
        "asgi": {"version": "3.0"},
        "scheme": "ws",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()],
        "client": ("test", 123),
        "server": ("test", 80),
        "subprotocols": [],
    }
    task = asyncio.create_task(app(scope, incoming.get, outgoing.put))
    return task, incoming, outgoing


async def test_real_websocket_handshake_fences(hosted):
    client, receiver, app = hosted
    for headers in (
        {},
        {"Authorization": "Bearer wrong"},
        {"Authorization": "Bearer operator-test-only"},
        {
            **receiver.identity.headers(),
            "Authorization": "Bearer operator-test-only",
            "X-AWF-Expected-Incarnation": "foreign",
        },
    ):
        task, _, outgoing = await websocket(app, headers)
        response = await asyncio.wait_for(outgoing.get(), 2)
        assert response["type"] == "websocket.close"
        assert response["code"] == 1008
        await task


async def test_active_websocket_rechecks_durable_owner(hosted):
    _, receiver, app = hosted
    entered, proceed = asyncio.Event(), asyncio.Event()

    from fastapi import WebSocket

    async def typed_stream(ws: WebSocket):
        await ws.accept()
        entered.set()
        await proceed.wait()
        await ws.send_text("protected")

    app.add_api_websocket_route("/test-protected-stream", typed_stream)
    task, _, outgoing = await websocket(
        app,
        {
            **receiver.identity.headers(),
            "Authorization": "Bearer operator-test-only",
        },
        path="/test-protected-stream",
    )
    assert (await asyncio.wait_for(outgoing.get(), 2))["type"] == "websocket.accept"
    await entered.wait()
    successor = await Receiver.start(receiver.config, receiver.factory)
    try:
        proceed.set()
        response = await asyncio.wait_for(outgoing.get(), 2)
        assert response["type"] == "websocket.close"
        await task
    finally:
        await successor.close()


async def test_existing_websocket_with_valid_identity(hosted):
    _, receiver, app = hosted
    task, _, outgoing = await websocket(
        app,
        {
            **receiver.identity.headers(),
            "Authorization": "Bearer operator-test-only",
        },
    )
    assert (await asyncio.wait_for(outgoing.get(), 2))["type"] == "websocket.accept"
    # The real endpoint subsequently closes for the deliberately absent workspace.
    await asyncio.wait_for(task, 3)


async def test_disabled_receiver_websocket_uses_close_frame(client):
    app = client._transport.app
    task, _, outgoing = await websocket(app, {}, path="/v1/hosted-node/identity")
    assert (await asyncio.wait_for(outgoing.get(), 2))["type"] == "websocket.close"
    await task
