"""Controller-only, exact-incarnation lifecycle contract."""

from typing import Annotated

from fastapi import APIRouter, Depends
from fastapi.security import HTTPBearer

from awf.hosted_node.api import (
    HOSTED_RESPONSES,
    MaterialRoute,
    ReceiverDependency,
    expected_identity_headers,
)
from awf.hosted_node.identity import MaterialDeniedError
from awf.hosted_node.lifecycle import NodeLifecycle
from awf.hosted_node.lifecycle_schemas import (
    BootChallenge,
    BootEvidence,
    Drain,
    LifecycleStatus,
    Maintenance,
    MaintenanceBegin,
    RecoveryAcknowledgement,
)

router = APIRouter(
    prefix="/v1/hosted-node/lifecycle",
    tags=["hosted-node"],
    route_class=MaterialRoute,
    responses=HOSTED_RESPONSES,
    dependencies=[
        Depends(expected_identity_headers),
        Depends(HTTPBearer(auto_error=False, scheme_name="bearerAuth")),
    ],
)


def lifecycle(receiver: ReceiverDependency) -> NodeLifecycle:
    if receiver.lifecycle is None:
        raise MaterialDeniedError("NODE_LIFECYCLE_DISABLED", 404)
    return receiver.lifecycle


Service = Annotated[NodeLifecycle, Depends(lifecycle)]


@router.post("/boot/evidence", response_model=BootEvidence)
async def boot_evidence(value: BootChallenge, service: Service) -> BootEvidence:
    return await service.boot_evidence(value.challenge)


@router.get("/readiness", response_model=LifecycleStatus)
async def readiness(service: Service) -> LifecycleStatus:
    return await service.readiness()


@router.post("/recovery/acknowledge", response_model=LifecycleStatus)
async def acknowledge(value: RecoveryAcknowledgement, service: Service) -> LifecycleStatus:
    return await service.acknowledge(value)


@router.post("/drain", response_model=LifecycleStatus)
async def drain(value: Drain, service: Service) -> LifecycleStatus:
    return await service.drain(value.generation)


@router.post("/maintenance/begin", response_model=LifecycleStatus)
async def begin_maintenance(value: MaintenanceBegin, service: Service) -> LifecycleStatus:
    return await service.begin_maintenance(value.generation, value.checkpoint)


@router.post("/maintenance/release", response_model=LifecycleStatus)
async def release_maintenance(value: Maintenance, service: Service) -> LifecycleStatus:
    return await service.release_maintenance(value.generation)


@router.post("/drain/abort", response_model=LifecycleStatus)
async def abort(value: Drain, service: Service) -> LifecycleStatus:
    return await service.abort(value.generation)
