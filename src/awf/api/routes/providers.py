"""Selected-provider discovery and isolated connection diagnostics."""

from __future__ import annotations

import os
from typing import Annotated

from fastapi import APIRouter, Depends, Header, Request
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.api.deps import get_db_session_factory, require_api_token
from awf.api.request_admission import (
    WORKSPACE_CREATE_ENDPOINT_FAMILY,
    RequestAdmissionDecision,
    admit_request_async,
)
from awf.api.responses import API_TOKEN_AUTH_ERROR_RESPONSES
from awf.api.schemas_providers import (
    ProviderCatalogResponse,
    ProviderConnectionTestRequest,
    ProviderConnectionTestResponse,
    ProviderQueryRequest,
)
from awf.common.config import Settings, get_settings
from awf.profiles.resolver import ProfileResolutionError
from awf.service.provider_catalog import ProviderCatalog
from awf.service.provider_diagnostics import create_connection_test, provider_error, query_material
from awf.service.workspaces import (
    WorkspaceCreateIdempotencyConflictError,
    WorkspaceCreateInsufficientDiskError,
    WorkspaceProviderReadinessBlockedError,
    WorkspaceService,
)

router = APIRouter(
    prefix="/v1/providers",
    tags=["providers"],
    dependencies=[Depends(require_api_token)],
    responses=API_TOKEN_AUTH_ERROR_RESPONSES,
)


class _RateLimitedError(Exception):
    def __init__(self, decision: RequestAdmissionDecision) -> None:
        self.decision = decision


@router.post("/models/query", response_model=ProviderCatalogResponse)
async def query_provider_models(
    payload: ProviderQueryRequest,
    request: Request,
) -> ProviderCatalogResponse | JSONResponse:
    try:
        profile, selected = query_material(payload, environ=os.environ)
        catalog = getattr(request.app.state, "provider_catalog", None)
        if catalog is None:
            catalog = ProviderCatalog()
            request.app.state.provider_catalog = catalog
        return await catalog.query(
            selected,
            environ=os.environ,
            context=str(payload.profile_ref or "") + ":" + profile.name,
        )
    except (ValueError, ProfileResolutionError):
        return JSONResponse(
            status_code=422,
            content=provider_error("PROVIDER_SELECTION_INVALID"),
        )


@router.post("/connection-tests", status_code=202, response_model=ProviderConnectionTestResponse)
async def test_provider_connection(
    payload: ProviderConnectionTestRequest,
    request: Request,
    factory: Annotated[async_sessionmaker[AsyncSession], Depends(get_db_session_factory)],
    settings: Annotated[Settings, Depends(get_settings)],
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> ProviderConnectionTestResponse | JSONResponse:
    if not idempotency_key or not idempotency_key.strip() or len(idempotency_key) > 255:
        return JSONResponse(
            status_code=400,
            content=provider_error("IDEMPOTENCY_KEY_REQUIRED"),
        )
    from awf.api.routes.workspaces import (
        _insufficient_disk_response,
        _workspace_admission_disk_check,
        _workspace_create_rate_limited_response,
    )

    disk_check = await _workspace_admission_disk_check(request, settings)

    async def admit() -> None:
        decision = await admit_request_async(
            request,
            endpoint_family=WORKSPACE_CREATE_ENDPOINT_FAMILY,
            limit=settings.workspace_create_rate_limit_count,
            window_seconds=settings.request_admission_window_seconds,
            reason_code="WORKSPACE_CREATE_RATE_LIMITED",
        )
        if not decision.allowed:
            raise _RateLimitedError(decision)

    try:
        return await create_connection_test(
            WorkspaceService(factory, settings=settings),
            payload,
            idempotency_key=idempotency_key.strip(),
            disk_check=disk_check,
            admission_check=admit,
        )
    except _RateLimitedError as error:
        return _workspace_create_rate_limited_response(error.decision)
    except WorkspaceCreateInsufficientDiskError:
        return _insufficient_disk_response(disk_check)
    except WorkspaceCreateIdempotencyConflictError:
        return JSONResponse(
            status_code=409,
            content=provider_error("IDEMPOTENCY_CONFLICT"),
        )
    except WorkspaceProviderReadinessBlockedError:
        return JSONResponse(
            status_code=409,
            content=provider_error("PROVIDER_READINESS_PRECHECK_FAILED"),
        )
    except (ValueError, ProfileResolutionError) as error:
        reason = str(error)
        if not reason.isupper() or len(reason) > 100:
            reason = "PROVIDER_SELECTION_INVALID"
        return JSONResponse(
            status_code=409,
            content=provider_error(reason),
        )
