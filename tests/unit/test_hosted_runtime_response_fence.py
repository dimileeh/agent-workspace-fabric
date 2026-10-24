"""Do not return a successful response after the accepted boot is superseded."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

from awf.hosted_node.auth import HostedGuard
from awf.hosted_node.identity import MaterialDeniedError
from awf.hosted_node.schemas import Identity


async def test_read_response_rechecks_current_identity():
    from awf.common.config import get_settings

    identity = Identity(owner="legacy/node", incarnation="123", recovery_generation=1, boot_epoch=2)
    receiver = SimpleNamespace(
        identity=identity,
        identity_status=AsyncMock(
            side_effect=[None, MaterialDeniedError("IDENTITY_MISMATCH", 403)]
        ),
        config=SimpleNamespace(
            controller_token=SimpleNamespace(get_secret_value=lambda: "synthetic")
        ),
    )
    app = SimpleNamespace(
        state=SimpleNamespace(hosted_receiver=receiver),
        dependency_overrides={
            get_settings: lambda: SimpleNamespace(api_token="operator", hosted_node=object())
        },
    )
    messages = []

    async def downstream(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b'{"workspace_id":"ws_private"}'})

    async def send(message):
        messages.append(message)

    scope = {
        "type": "http",
        "app": app,
        "path": "/v1/workspaces/ws_private",
        "method": "GET",
        "headers": [
            (k.lower().encode(), v.encode())
            for k, v in {
                "Authorization": "Bearer synthetic",
                **identity.headers(),
            }.items()
        ],
    }
    await HostedGuard(downstream)(scope, AsyncMock(), send)
    assert messages[0]["status"] == 403
    assert b"ws_private" not in b"".join(item.get("body", b"") for item in messages)
