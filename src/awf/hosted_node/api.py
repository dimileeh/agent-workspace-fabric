"""Canonical receiver API; errors never serialize submitted material."""

from collections.abc import Callable, Coroutine
from typing import Annotated, Any, cast

from fastapi import APIRouter, Depends, FastAPI, Header, Request
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute
from fastapi.security import HTTPBearer
from sqlalchemy.exc import SQLAlchemyError
from starlette.responses import JSONResponse, Response

from awf.hosted_node.auth import PREFIX
from awf.hosted_node.identity import MaterialDeniedError
from awf.hosted_node.receiver import Receiver
from awf.hosted_node.schemas import (
    Acknowledgement,
    Binding,
    IdentityStatus,
    Install,
    MaterialStatus,
    Preparation,
    Refresh,
)


class MaterialRoute(APIRoute):
    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        original = super().get_route_handler()

        async def safe(request: Request) -> Response:
            try:
                return await original(request)
            except (SQLAlchemyError, OSError, TimeoutError):
                return JSONResponse({"error_code": "AUTHORITY_UNAVAILABLE"}, status_code=503)

        return safe


def expected_identity_headers(
    _owner: Annotated[str, Header(alias="X-AWF-Expected-Owner")],
    _incarnation: Annotated[str, Header(alias="X-AWF-Expected-Incarnation")],
    _recovery: Annotated[str, Header(alias="X-AWF-Expected-Recovery-Generation")],
    _boot: Annotated[str, Header(alias="X-AWF-Expected-Boot-Epoch")],
) -> None:
    """Schema declarations only; the ASGI guard enforces these before parsing."""
    return


router = APIRouter(
    route_class=MaterialRoute,
    prefix=PREFIX,
    tags=["hosted-node"],
    dependencies=[Depends(HTTPBearer(auto_error=False, scheme_name="bearerAuth"))],
    responses={
        401: {"description": "Bearer denied"},
        403: {"description": "Identity or controller role denied"},
        404: {"description": "Disabled or preparation absent"},
        409: {"description": "Exact binding or monotonic authority conflict"},
        413: {"description": "Body exceeds 128 KiB"},
        422: {"description": "Sanitized invalid request"},
        503: {"description": "Authority unavailable; acceptance is unknown"},
    },
)


def receiver(request: Request) -> Receiver:
    return cast(Receiver, request.app.state.hosted_receiver)


ReceiverDependency = Annotated[Receiver, Depends(receiver)]


@router.get("/identity", response_model=IdentityStatus)
async def identity(service: ReceiverDependency) -> IdentityStatus:
    return await service.identity_status()


@router.post(
    dependencies=[Depends(expected_identity_headers)],
    path="/identity/acknowledge",
    response_model=IdentityStatus,
)
async def acknowledge(value: Acknowledgement, service: ReceiverDependency) -> IdentityStatus:
    return await service.identity_status(value.idempotency_key)


@router.post(dependencies=[Depends(expected_identity_headers)], path="/preparations")
async def prepare(value: Preparation, service: ReceiverDependency) -> dict[str, str]:
    await service.prepare(value)
    return {"status": "prepared"}


@router.post(
    dependencies=[Depends(expected_identity_headers)],
    path="/materials/install",
    response_model=MaterialStatus,
)
async def install(value: Install, service: ReceiverDependency) -> MaterialStatus:
    return await service.install(value)


@router.post(
    dependencies=[Depends(expected_identity_headers)],
    path="/materials/status",
    response_model=MaterialStatus,
)
async def material_status(value: Binding, service: ReceiverDependency) -> MaterialStatus:
    return await service.status(value)


@router.post(
    dependencies=[Depends(expected_identity_headers)],
    path="/materials/refresh",
    response_model=MaterialStatus,
)
async def refresh(value: Refresh, service: ReceiverDependency) -> MaterialStatus:
    return await service.install(value, refresh=True)


@router.post(
    dependencies=[Depends(expected_identity_headers)],
    path="/materials/revoke",
    response_model=MaterialStatus,
)
async def revoke(value: Binding, service: ReceiverDependency) -> MaterialStatus:
    return await service.revoke(value)


async def denial_error(_request: Request, exc: Exception) -> JSONResponse:
    denial = cast(MaterialDeniedError, exc)
    return JSONResponse({"error_code": denial.reason}, status_code=denial.status)


async def validation_error(request: Request, exc: Exception) -> JSONResponse:
    if request.url.path.startswith(PREFIX + "/"):
        return JSONResponse({"error_code": "INVALID_REQUEST"}, status_code=422)
    from fastapi.exception_handlers import request_validation_exception_handler

    return await request_validation_exception_handler(request, cast(RequestValidationError, exc))


def configure(app: FastAPI) -> None:
    from awf.hosted_node.auth import HostedGuard
    from awf.hosted_node.lifecycle_api import router as lifecycle_router

    app.description += (
        "\n\nProtected hosted-node mode is default-disabled and runtime activation is CLOSED. "
        "When enabled, all HTTP routes and WebSocket handshakes except GET /healthz and "
        "the authenticated GET /v1/hosted-node/identity bootstrap require exact "
        "X-AWF-Expected-Owner, X-AWF-Expected-Incarnation, "
        "X-AWF-Expected-Recovery-Generation and X-AWF-Expected-Boot-Epoch headers. "
        "These are expectations, never server identity inputs. Existing APIs retain operator "
        "bearer authentication; hosted mutations require the separate trusted controller bearer. "
        "See docs/contracts/hosted-node-material.md for CLOSED admission and transport requirements."
    )
    app.add_middleware(HostedGuard)
    app.include_router(router)
    app.include_router(lifecycle_router)
    app.add_exception_handler(MaterialDeniedError, denial_error)
    # Database errors are sanitized only on receiver routes by MaterialRoute.
    app.add_exception_handler(RequestValidationError, validation_error)
