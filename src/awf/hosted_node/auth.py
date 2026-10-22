"""App-wide HTTP/WS guard. Default-deny execution while runtime integration is absent."""

import hmac

from sqlalchemy.exc import SQLAlchemyError
from starlette.datastructures import Headers
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from awf.common.config import get_settings
from awf.hosted_node.identity import MaterialDeniedError

PREFIX = "/v1/hosted-node"
# Only these existing mutations are safe without hosted execution integration.
SAFE_CONTROLS = frozenset({"cancel", "stop"})


class HostedGuard:
    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in {"http", "websocket"}:
            await self.app(scope, receive, send)
            return
        application = scope["app"]
        receiver = getattr(application.state, "hosted_receiver", None)
        settings = application.dependency_overrides.get(get_settings, get_settings)()
        path = scope["path"]
        dedicated = path == PREFIX or path.startswith(PREFIX + "/")
        if receiver is None and settings.hosted_node is None:
            if dedicated and scope["type"] == "websocket":
                await send({"type": "websocket.close", "code": 1008, "reason": "NOT_FOUND"})
            elif dedicated:
                await JSONResponse({"error_code": "NOT_FOUND"}, status_code=404)(
                    scope, receive, send
                )
            else:
                await self.app(scope, receive, send)
            return
        if path == "/healthz" and scope["type"] == "http" and scope["method"] == "GET":
            await self.app(scope, receive, send)
            return

        async def deny(reason: str, status: int) -> None:
            if scope["type"] == "websocket":
                await send({"type": "websocket.close", "code": 1008, "reason": reason})
            else:
                await JSONResponse({"error_code": reason}, status_code=status)(scope, receive, send)

        try:
            if receiver is None:
                raise MaterialDeniedError("AUTHORITY_UNAVAILABLE", 503)
            if hmac.compare_digest(
                (settings.api_token or "").encode(),
                receiver.config.controller_token.get_secret_value().encode(),
            ):
                raise MaterialDeniedError("CONTROLLER_TOKEN_MUST_DIFFER", 503)
            headers = Headers(scope=scope)
            supplied = headers.get("authorization", "")
            scheme, _, token = supplied.partition(" ")
            controller = scheme.lower() == "bearer" and hmac.compare_digest(
                token.encode(), receiver.config.controller_token.get_secret_value().encode()
            )
            operator = (
                bool(settings.api_token)
                and scheme.lower() == "bearer"
                and hmac.compare_digest(token.encode(), settings.api_token.encode())
            )
            if not (controller or operator):
                raise MaterialDeniedError("UNAUTHORIZED", 401)
            bootstrap = path == PREFIX + "/identity" and scope.get("method") == "GET"
            if dedicated and not bootstrap and not controller:
                raise MaterialDeniedError("CONTROLLER_REQUIRED", 403)
            if not bootstrap and any(
                headers.get(key) != value for key, value in receiver.identity.headers().items()
            ):
                raise MaterialDeniedError("IDENTITY_MISMATCH", 403)
            await receiver.identity_status()
            safe = False
            if not dedicated and scope["type"] == "http" and scope["method"] not in {"GET", "HEAD"}:
                safe = (
                    scope["method"] == "POST"
                    and path.startswith("/v1/workspaces/")
                    and len(path.strip("/").split("/")) == 4
                    and path.rsplit("/", 1)[-1] in SAFE_CONTROLS
                )
                safe = safe or (
                    scope["method"] == "DELETE"
                    and path.startswith("/v1/workspaces/")
                    and len(path.strip("/").split("/")) == 3
                )
                if not safe:
                    raise MaterialDeniedError("HOSTED_RUNTIME_INTEGRATION_REQUIRED", 403)
            if (dedicated or safe) and scope["type"] == "http":
                body = bytearray()
                while True:
                    message = await receive()
                    if message["type"] == "http.disconnect":
                        return
                    chunk = message.get("body", b"")
                    if len(body) + len(chunk) > 131072:
                        raise MaterialDeniedError("BODY_TOO_LARGE", 413)
                    body.extend(chunk)
                    if not message.get("more_body"):
                        break

                async def buffered() -> Message:
                    return {"type": "http.request", "body": bytes(body), "more_body": False}

                receive = buffered
                if safe:
                    # Body delivery may outlive this process's durable ownership.
                    await receiver.identity_status()
        except MaterialDeniedError as exc:
            await deny(exc.reason, exc.status)
            return
        except (SQLAlchemyError, OSError, TimeoutError):
            await deny("AUTHORITY_UNAVAILABLE", 503)
            return

        async def fenced_send(message: Message) -> None:
            if scope["type"] == "websocket" and message["type"] in {
                "websocket.accept",
                "websocket.send",
            }:
                try:
                    await receiver.identity_status()
                except (MaterialDeniedError, SQLAlchemyError, OSError, TimeoutError):
                    await deny("IDENTITY_UNAVAILABLE", 403)
                    raise _StreamFencedError from None
            await send(message)

        try:
            await self.app(scope, receive, fenced_send)
        except _StreamFencedError:
            return


class _StreamFencedError(Exception):
    """Internal stream unwind after a safe close frame."""
