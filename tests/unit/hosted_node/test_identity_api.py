import pytest

from awf.hosted_node.receiver import Receiver


async def test_disabled(client):
    response = await client.get("/v1/hosted-node/identity")
    assert response.status_code == 404


async def test_identity_and_acknowledgement(hosted):
    client, receiver, _ = hosted
    response = await client.get("/v1/hosted-node/identity")
    assert response.status_code == 200
    assert response.json()["owner"] == "owner"
    assert response.json()["activation"] == "closed"
    response = await client.post(
        "/v1/hosted-node/identity/acknowledge", json={"idempotency_key": "ack"}
    )
    assert response.status_code == 200
    assert response.json()["acknowledged"] is True
    response = await client.get(
        "/v1/workspaces", headers={"Authorization": "Bearer operator-test-only"}
    )
    assert response.status_code == 200
    for header in receiver.identity.headers():
        saved = client.headers.pop(header)
        assert (await client.get("/v1/workspaces")).status_code == 403
        client.headers[header] = "foreign"
        assert (await client.get("/v1/workspaces")).status_code == 403
        client.headers[header] = saved
    client.headers["Authorization"] = "Bearer wrong"
    assert (await client.get("/v1/hosted-node/identity")).status_code == 401
    client.headers["Authorization"] = "Bearer operator-test-only"
    assert (await client.post("/v1/hosted-node/preparations", json={})).status_code == 403


@pytest.mark.parametrize(
    "path",
    [
        "/v1/workspaces",
        "/v1/workspaces/adopt-pr",
        "/v1/workspaces/w/retry",
        "/v1/workspaces/w/remonitor",
        "/v1/workspaces/w/guide",
        "/v1/workspaces/w/refresh",
        "/v1/workspaces/w/validate",
        "/v1/workspaces/w/rebase",
    ],
)
async def test_closed_before_body_or_effects(hosted, path):
    client, _, _ = hosted
    response = await client.post(path, content=b"invalid json")
    assert response.status_code == 403
    assert response.json()["error_code"] == "HOSTED_RUNTIME_INTEGRATION_REQUIRED"


async def test_restart_supersedes_old_process(hosted):
    client, receiver, app = hosted
    successor = await Receiver.start(receiver.config, receiver.factory)
    try:
        assert successor.identity.boot_epoch > receiver.identity.boot_epoch
        assert (await client.get("/v1/hosted-node/identity")).status_code == 403
        app.state.hosted_receiver = successor
        assert (await client.get("/v1/workspaces")).status_code == 403
        client.headers.update(successor.identity.headers())
        assert (await client.get("/v1/hosted-node/identity")).status_code == 200
        with pytest.raises(ValueError, match="IDENTITY_ROLLBACK"):
            await Receiver.start(
                receiver.config.model_copy(update={"incarnation": "other"}), receiver.factory
            )
    finally:
        await successor.close()


async def test_existing_controls_reads_and_route_inventory(hosted):
    client, receiver, app = hosted
    protected_paths = [
        "/v1/workspaces/missing",
        "/v1/workspaces/missing/secret-leases",
        "/v1/workspaces/missing/artifacts",
        "/v1/workspaces/missing/cancel",
        "/v1/workspaces/missing/stop",
    ]
    for path in protected_paths:
        method = "POST" if path.endswith(("/cancel", "/stop")) else "GET"
        response = await client.request(method, path, headers={"X-AWF-Expected-Boot-Epoch": "0"})
        assert response.status_code == 403
    # Every registered mutation outside the explicit cleanup whitelist is closed,
    # including callbacks/locks/queue operations that might indirectly cause work.
    for route in app.routes:
        if "POST" not in getattr(route, "methods", set()):
            continue
        path = route.path
        if path.startswith("/v1/hosted-node/") or path.endswith(("/cancel", "/stop")):
            continue
        response = await client.post(path.replace("{workspace_id}", "missing"), content=b"bad")
        assert response.status_code == 403
        assert response.json()["error_code"] == "HOSTED_RUNTIME_INTEGRATION_REQUIRED"


async def test_generation_advance_and_rollback(hosted):
    client, receiver, app = hosted
    newer_config = receiver.config.model_copy(
        update={"incarnation": "vm2", "recovery_generation": 2}
    )
    successor = await Receiver.start(newer_config, receiver.factory)
    try:
        with pytest.raises(ValueError, match="IDENTITY_ROLLBACK"):
            await Receiver.start(receiver.config, receiver.factory)
        assert (await client.get("/v1/hosted-node/identity")).status_code == 403
    finally:
        await successor.close()


async def test_unavailable_authority_denies_before_any_endpoint(hosted, monkeypatch):
    client, receiver, _ = hosted

    async def unavailable(*args, **kwargs):
        raise OSError("private-error-detail")

    monkeypatch.setattr(receiver, "identity_status", unavailable)
    for path in ("/v1/hosted-node/identity", "/v1/workspaces"):
        response = await client.get(path)
        assert response.status_code == 503
        assert "private-error-detail" not in response.text


async def test_production_lifespan_wires_real_owner_state(hosted, engine, monkeypatch):
    from httpx import ASGITransport, AsyncClient

    from awf.api import app as app_module
    from awf.common.config import Settings, get_settings

    _, receiver, _ = hosted
    settings = Settings(_env_file=None, hosted_node=receiver.config, api_token="operator-test-only")
    monkeypatch.setattr(app_module, "get_settings", lambda: settings)
    monkeypatch.setattr(app_module, "make_engine", lambda _url: engine)
    app = app_module.create_app()
    app.dependency_overrides[get_settings] = lambda: settings
    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client,
    ):
        response = await client.get(
            "/v1/hosted-node/identity", headers={"Authorization": "Bearer controller-test-only"}
        )
        assert response.status_code == 200
        assert response.json()["boot_epoch"] > receiver.identity.boot_epoch
        current = app.state.hosted_receiver
    assert current.closed
    with pytest.raises(ValueError, match="AUTHORITY_UNAVAILABLE"):
        await current.identity_status()


async def test_controller_token_must_be_distinct(hosted):
    from awf.common.config import Settings, get_settings

    client, _, app = hosted
    app.dependency_overrides[get_settings] = lambda: Settings(
        _env_file=None,
        api_token="controller-test-only",
    )
    response = await client.get("/v1/hosted-node/identity")
    assert response.status_code == 503
    assert response.json()["error_code"] == "CONTROLLER_TOKEN_MUST_DIFFER"


async def test_bootstrap_headers_are_not_authority_and_uninitialized_mode_is_closed(hosted):
    from awf.common.config import Settings, get_settings

    client, receiver, app = hosted
    response = await client.get(
        "/v1/hosted-node/identity", headers={"X-AWF-Expected-Owner": "forged"}
    )
    assert response.status_code == 200 and response.json()["owner"] == "owner"
    assert (await client.get("/healthz", headers={"Authorization": ""})).status_code == 200
    app.dependency_overrides[get_settings] = lambda: Settings(
        _env_file=None,
        hosted_node=receiver.config,
        api_token="operator-test-only",
    )
    del app.state.hosted_receiver
    try:
        assert (await client.get("/v1/workspaces")).status_code == 503
        assert (await client.get("/v1/hosted-node/identity")).status_code == 503
    finally:
        app.state.hosted_receiver = receiver
