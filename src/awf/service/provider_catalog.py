"""Bounded catalog claims, scoped by frozen connection and query context."""

from __future__ import annotations

import asyncio
import json
from collections import OrderedDict
from collections.abc import Mapping
from datetime import UTC, datetime
from time import monotonic
from typing import Any

import httpx

from awf.api.schemas_providers import ProviderCatalogResponse, ProviderModel
from awf.common.agent_execution import ExecutionSelection

CATALOG_TTL_SECONDS = 300
MAX_RESPONSE_BYTES = 2_000_000
MAX_MODELS = 5000
MAX_CACHE_ENTRIES = 128


class ProviderCatalog:
    def __init__(self, *, client: httpx.AsyncClient | None = None) -> None:
        self._client = client
        self._cache: OrderedDict[tuple[str, ...], tuple[float, ProviderCatalogResponse]] = (
            OrderedDict()
        )
        self._lock = asyncio.Lock()

    @property
    def cache_keys(self) -> tuple[tuple[str, ...], ...]:
        return tuple(self._cache)

    async def query(
        self, selection: ExecutionSelection, *, environ: Mapping[str, str], context: str = ""
    ) -> ProviderCatalogResponse:
        scope = selection.connection_scope(
            "catalog:" + context if selection.network_service else None
        )
        base: dict[str, Any] = {
            "checked_at": datetime.now(UTC).isoformat(),
            "connection_scope": scope,
            "metadata": {
                "agent": selection.agent,
                "provider": selection.provider,
                "endpoint": selection.endpoint,
                "credential_ref": selection.credential_ref,
            },
        }
        if not selection.third_party:
            return ProviderCatalogResponse(
                status="unsupported", reason_code="PROVIDER_DISCOVERY_UNSUPPORTED", **base
            )
        if selection.network_service:
            return ProviderCatalogResponse(
                status="unsupported", reason_code="PROVIDER_DISCOVERY_NETWORK_UNSUPPORTED", **base
            )
        key = (scope, selection.agent, selection.provider, context)
        async with self._lock:
            now = monotonic()
            cached = self._cache.get(key)
            if cached and now - cached[0] < CATALOG_TTL_SECONDS:
                self._cache.move_to_end(key)
                return cached[1].model_copy(deep=True, update={"cached": True})
            try:
                headers = {}
                if selection.provider == "openrouter":
                    credential = environ.get(str(selection.credential_ref).removeprefix("env/"))
                    if not credential:
                        raise ValueError("PROVIDER_CREDENTIAL_MISSING")
                    headers["Authorization"] = "Bearer " + credential
                path = "/api/tags" if selection.provider == "ollama" else "/v1/models"
                async with asyncio.timeout(10):
                    if self._client is not None:
                        data = await self._fetch(
                            self._client, str(selection.endpoint) + path, headers
                        )
                    else:
                        async with httpx.AsyncClient(
                            timeout=10, follow_redirects=False, trust_env=False
                        ) as client:
                            data = await self._fetch(
                                client, str(selection.endpoint) + path, headers
                            )
                raw = data["models" if selection.provider == "ollama" else "data"]
                if not isinstance(raw, list) or len(raw) > MAX_MODELS:
                    raise ValueError("PROVIDER_DISCOVERY_RESPONSE_INVALID")
                models = []
                for item in raw:
                    ident = item["name" if selection.provider == "ollama" else "id"]
                    if not isinstance(ident, str) or not ident or len(ident) > 500:
                        raise ValueError("PROVIDER_DISCOVERY_RESPONSE_INVALID")
                    version = item.get("digest")
                    context_length = item.get("context_length")
                    params = item.get("supported_parameters", item.get("capabilities"))
                    pricing = item.get("pricing")
                    models.append(
                        ProviderModel(
                            id=ident,
                            version=version
                            if isinstance(version, str) and len(version) < 500
                            else None,
                            context_length=context_length
                            if type(context_length) is int and context_length > 0
                            else None,
                            tools="tools" in params if isinstance(params, list) else None,
                            pricing={
                                k: str(v)[:100]
                                for k, v in pricing.items()
                                if k in {"prompt", "completion"}
                            }
                            if isinstance(pricing, dict)
                            else None,
                        )
                    )
                # Do not trust catalog strings with credential material, even on a compromised endpoint.
                secrets = (
                    [environ.get(str(selection.credential_ref).removeprefix("env/"), "")]
                    if selection.credential_ref
                    else []
                )
                if any(
                    secret and secret in json.dumps([m.model_dump() for m in models])
                    for secret in secrets
                ):
                    raise ValueError("PROVIDER_DISCOVERY_RESPONSE_INVALID")
                result = ProviderCatalogResponse(
                    status="available",
                    reason_code="PROVIDER_DISCOVERY_AVAILABLE",
                    models=models,
                    **base,
                )
                self._cache[key] = (now, result)
                self._cache.move_to_end(key)
                while len(self._cache) > MAX_CACHE_ENTRIES:
                    self._cache.popitem(last=False)
                return result
            except httpx.HTTPStatusError as error:
                return ProviderCatalogResponse(
                    status="unavailable",
                    reason_code="PROVIDER_DISCOVERY_UNAVAILABLE",
                    http_status=error.response.status_code,
                    **base,
                )
            except (httpx.HTTPError, TimeoutError, ValueError, KeyError, TypeError):
                return ProviderCatalogResponse(
                    status="unavailable", reason_code="PROVIDER_DISCOVERY_UNAVAILABLE", **base
                )

    @staticmethod
    async def _fetch(
        client: httpx.AsyncClient, url: str, headers: dict[str, str]
    ) -> dict[str, Any]:
        async with client.stream(
            "GET", url, headers=headers, timeout=10, follow_redirects=False
        ) as response:
            response.raise_for_status()
            chunks = bytearray()
            async for chunk in response.aiter_bytes():
                chunks.extend(chunk)
                if len(chunks) > MAX_RESPONSE_BYTES:
                    raise ValueError("PROVIDER_DISCOVERY_RESPONSE_INVALID")
        data = json.loads(chunks)
        if not isinstance(data, dict):
            raise ValueError("PROVIDER_DISCOVERY_RESPONSE_INVALID")
        return data
