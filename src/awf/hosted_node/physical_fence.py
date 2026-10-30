"""Bounded, server-owned physical-fence lookup; not a recovery authorization."""

import asyncio
import json
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import httpx
from pydantic import AwareDatetime, Field, field_validator

from awf.hosted_node.host_config import PhysicalFenceAuthoritySettings
from awf.hosted_node.host_runtime import PhysicalFenceEvidence
from awf.hosted_node.schemas import Identity, Opaque, StrictModel

_MAX_BYTES = 8192
_TIMEOUT_SECONDS = 2


class _Lookup(StrictModel):
    previous_identity: Identity
    identity: Identity
    receipt_ref: Opaque
    challenge: str = Field(min_length=36, max_length=36)

    @field_validator("receipt_ref")
    @classmethod
    def canonical_receipt(cls, value: str) -> str:
        receipt = UUID(value)
        if str(receipt) != value or receipt.int == 0:
            raise ValueError("CANONICAL_RECEIPT_UUID_REQUIRED")
        return value


class _Response(_Lookup):
    expires_at: AwareDatetime


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("DUPLICATE_AUTHORITY_FIELD")
        result[key] = value
    return result


def physical_fence_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        verify=True,
        trust_env=False,
        follow_redirects=False,
        timeout=_TIMEOUT_SECONDS,
        limits=httpx.Limits(max_connections=1, max_keepalive_connections=0),
    )


class PhysicalFenceVerifier:
    def __init__(
        self, config: PhysicalFenceAuthoritySettings, owner: str, client: httpx.AsyncClient
    ) -> None:
        self._config, self._owner, self._client = config, owner, client

    async def verify(
        self, previous_identity: Identity, identity: Identity, receipt_ref: str
    ) -> PhysicalFenceEvidence | None:
        try:
            async with asyncio.timeout(_TIMEOUT_SECONDS):
                if previous_identity.owner != self._owner or identity.owner != self._owner:
                    return None
                lookup = _Lookup(
                    previous_identity=previous_identity,
                    identity=identity,
                    receipt_ref=receipt_ref,
                    challenge=str(uuid4()),
                )
                async with self._client.stream(
                    "POST",
                    self._config.url,
                    json=lookup.model_dump(),
                    headers={
                        "Authorization": "Bearer " + self._config.bearer.get_secret_value(),
                        "Accept": "application/json",
                        "Accept-Encoding": "identity",
                    },
                    follow_redirects=False,
                ) as response:
                    if (
                        response.status_code != 200
                        or response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
                        != "application/json"
                        or response.headers.get("content-encoding", "identity").lower()
                        != "identity"
                    ):
                        return None
                    length = response.headers.get("content-length")
                    if length is not None and not 0 <= int(length) <= _MAX_BYTES:
                        return None
                    body = bytearray()
                    async for chunk in response.aiter_raw():
                        if len(body) + len(chunk) > _MAX_BYTES:
                            return None
                        body.extend(chunk)
                # JSON parsers otherwise silently choose one of conflicting fields.
                json.loads(body, object_pairs_hook=_unique_object)
                result = _Response.model_validate_json(body, strict=True)
                now = datetime.now(UTC)
                if (
                    result.previous_identity != lookup.previous_identity
                    or result.identity != lookup.identity
                    or result.receipt_ref != lookup.receipt_ref
                    or result.challenge != lookup.challenge
                    or not now < result.expires_at <= now + timedelta(seconds=30)
                ):
                    return None
                return PhysicalFenceEvidence(
                    result.previous_identity, result.identity, result.receipt_ref, result.expires_at
                )
        except Exception:
            # Authority responses and transport errors may contain credentials or
            # provider data. Cancellation (BaseException) deliberately escapes.
            return None
