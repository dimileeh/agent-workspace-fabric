"""OpenRouter key-management client used only by the API and worker processes.

The management key authenticates key provisioning; it is never forwarded to
agent stacks, hosted payloads, logs, argv or the database. Minted key values
are returned once, held in memory for one invocation and never persisted.
Errors carry a reason code, operation and HTTP status only, never bodies.
"""

from __future__ import annotations

import json
import math
import os
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from decimal import ROUND_DOWN, Decimal, InvalidOperation
from typing import Any

import httpx

OPENROUTER_MANAGEMENT_KEY_ENV = "OPENROUTER_MANAGEMENT_KEY"
OPENROUTER_MANAGEMENT_BASE_URL = "https://openrouter.ai/api/v1"
MANAGEMENT_TIMEOUT_SECONDS = 10.0
MAX_RESPONSE_BYTES = 2_000_000
LIST_PAGE_SIZE = 100
DEFAULT_MAX_LIST_PAGES = 50
_MAX_FIELD_LENGTH = 500
USD_QUANTUM = Decimal("0.000001")

MANAGEMENT_API_UNAUTHORIZED = "PROVIDER_MANAGEMENT_API_UNAUTHORIZED"
MANAGEMENT_API_RATE_LIMITED = "PROVIDER_MANAGEMENT_API_RATE_LIMITED"
MANAGEMENT_API_UNAVAILABLE = "PROVIDER_MANAGEMENT_API_UNAVAILABLE"
MANAGEMENT_API_REJECTED = "PROVIDER_MANAGEMENT_API_REJECTED"
MANAGEMENT_API_RESPONSE_INVALID = "PROVIDER_MANAGEMENT_API_RESPONSE_INVALID"
MANAGED_KEY_NOT_FOUND = "PROVIDER_MANAGED_KEY_NOT_FOUND"
PROVIDER_MANAGEMENT_CREDENTIAL_FORBIDDEN = "PROVIDER_MANAGEMENT_CREDENTIAL_FORBIDDEN"
_MANAGEMENT_REFERENCE_RE = re.compile(
    r"\$\{?" + OPENROUTER_MANAGEMENT_KEY_ENV + r"(?![A-Za-z0-9_])"
)


def scrub_openrouter_management_environ() -> None:
    """Drop the management key from ``os.environ`` once settings have resolved it.

    Docker Compose, git and agent subprocesses inherit the worker environment;
    removing the variable keeps them from interpolating or inheriting it.
    """
    os.environ.pop(OPENROUTER_MANAGEMENT_KEY_ENV, None)


def references_management_key(value: str | None) -> bool:
    """Whether a profile value or env-secret source names the management key."""
    if not value:
        return False
    stripped = value.strip().removeprefix("env/")
    return stripped == OPENROUTER_MANAGEMENT_KEY_ENV or bool(_MANAGEMENT_REFERENCE_RE.search(value))


def reject_management_credential_references(
    *, values: Iterable[str | None], sources: Iterable[str | None] = ()
) -> None:
    """Refuse workspace material that would interpolate or lease the management key."""
    if any(references_management_key(value) for value in (*values, *sources)):
        raise ValueError(PROVIDER_MANAGEMENT_CREDENTIAL_FORBIDDEN)


def usd(value: Decimal | float | int | str) -> Decimal:
    """Quantize a USD amount to the 6 dp AWF persists and sends, never rounding up."""
    return Decimal(str(value)).quantize(USD_QUANTUM, rounding=ROUND_DOWN)


class OpenRouterManagementError(Exception):
    """A classified management-API failure; the message never echoes a response."""

    def __init__(self, reason_code: str, *, operation: str, http_status: int | None = None):
        self.reason_code = reason_code
        self.operation = operation
        self.http_status = http_status
        status = f" (HTTP {http_status})" if http_status is not None else ""
        super().__init__(f"{reason_code}: OpenRouter key {operation} failed{status}")


@dataclass(frozen=True)
class KeyInfo:
    """Non-secret key identity and USD counters reported by the management API."""

    hash: str
    name: str
    disabled: bool
    limit: Decimal | None
    limit_remaining: Decimal | None
    usage: Decimal
    byok_usage: Decimal
    created_at: str | None = None

    @property
    def spend(self) -> Decimal:
        """Usage counted against the limit (``include_byok_in_limit=true``)."""
        return self.usage + self.byok_usage

    @property
    def exhausted(self) -> bool:
        if self.limit_remaining is not None and self.limit_remaining <= 0:
            return True
        return self.limit is not None and self.spend >= self.limit


@dataclass(frozen=True)
class MintedKey:
    """A freshly minted key; ``key`` is returned once and never persisted."""

    info: KeyInfo
    key: str = field(repr=False)


@dataclass(frozen=True)
class KeyListing:
    keys: tuple[KeyInfo, ...]
    truncated: bool


class OpenRouterManagementClient:
    """Bounded client for ``/api/v1/keys``; no redirects, proxies or body logging."""

    def __init__(
        self,
        management_key: str,
        *,
        base_url: str = OPENROUTER_MANAGEMENT_BASE_URL,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout_seconds: float = MANAGEMENT_TIMEOUT_SECONDS,
    ) -> None:
        if not management_key or not management_key.strip():
            raise ValueError("PROVIDER_MANAGEMENT_KEY_MISSING")
        self._management_key = management_key.strip()
        self._base_url = base_url.rstrip("/")
        self._transport = transport
        self._timeout_seconds = timeout_seconds

    def __repr__(self) -> str:
        return f"OpenRouterManagementClient(base_url={self._base_url!r})"

    async def create_key(self, name: str, limit_usd: Decimal) -> MintedKey:
        body = {"name": name, "limit": float(usd(limit_usd)), "include_byok_in_limit": True}
        data = await self._request("create", "POST", "/keys", json_body=body, ok=(200, 201))
        key = data.get("key")
        if not isinstance(key, str) or not key or len(key) > _MAX_FIELD_LENGTH:
            raise OpenRouterManagementError(MANAGEMENT_API_RESPONSE_INVALID, operation="create")
        info = self._key_info(data.get("data"), operation="create", secrets=(key,))
        return MintedKey(info=info, key=key)

    async def get_key(self, key_hash: str) -> KeyInfo:
        data = await self._request("get", "GET", f"/keys/{_path(key_hash)}")
        return self._key_info(data.get("data"), operation="get")

    async def update_key_limit(self, key_hash: str, limit_usd: Decimal) -> KeyInfo:
        data = await self._request(
            "update",
            "PATCH",
            f"/keys/{_path(key_hash)}",
            json_body={"limit": float(usd(limit_usd))},
        )
        return self._key_info(data.get("data"), operation="update")

    async def delete_key(self, key_hash: str) -> bool:
        """Delete a key; ``False`` means it was already gone (HTTP 404)."""
        try:
            await self._request("delete", "DELETE", f"/keys/{_path(key_hash)}")
        except OpenRouterManagementError as error:
            if error.reason_code == MANAGED_KEY_NOT_FOUND:
                return False
            raise
        return True

    async def list_keys(self, *, max_pages: int = DEFAULT_MAX_LIST_PAGES) -> KeyListing:
        """List keys page by page (100 per page, no cursor), bounded by ``max_pages``."""
        keys: list[KeyInfo] = []
        for page in range(max_pages):
            data = await self._request(
                "list",
                "GET",
                "/keys",
                params={"offset": str(page * LIST_PAGE_SIZE), "include_disabled": "true"},
            )
            items = data.get("data")
            if not isinstance(items, list) or len(items) > LIST_PAGE_SIZE:
                raise OpenRouterManagementError(MANAGEMENT_API_RESPONSE_INVALID, operation="list")
            keys.extend(self._key_info(item, operation="list") for item in items)
            if len(items) < LIST_PAGE_SIZE:
                return KeyListing(keys=tuple(keys), truncated=False)
        return KeyListing(keys=tuple(keys), truncated=True)

    async def _request(
        self,
        operation: str,
        method: str,
        path: str,
        *,
        json_body: Mapping[str, Any] | None = None,
        params: Mapping[str, str] | None = None,
        ok: tuple[int, ...] = (200,),
    ) -> dict[str, Any]:
        headers = {"Authorization": f"Bearer {self._management_key}"}
        try:
            async with (
                httpx.AsyncClient(
                    transport=self._transport,
                    timeout=self._timeout_seconds,
                    follow_redirects=False,
                    trust_env=False,
                ) as client,
                client.stream(
                    method,
                    self._base_url + path,
                    headers=headers,
                    json=json_body,
                    params=params,
                ) as response,
            ):
                status = response.status_code
                if status not in ok:
                    raise OpenRouterManagementError(
                        _status_reason(status), operation=operation, http_status=status
                    )
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    body.extend(chunk)
                    if len(body) > MAX_RESPONSE_BYTES:
                        raise OpenRouterManagementError(
                            MANAGEMENT_API_RESPONSE_INVALID, operation=operation
                        )
        except httpx.HTTPError:
            # Transport errors can carry request headers; never chain them.
            raise OpenRouterManagementError(
                MANAGEMENT_API_UNAVAILABLE, operation=operation
            ) from None
        try:
            data = json.loads(body)
        except ValueError:
            raise OpenRouterManagementError(
                MANAGEMENT_API_RESPONSE_INVALID, operation=operation
            ) from None
        if not isinstance(data, dict):
            raise OpenRouterManagementError(MANAGEMENT_API_RESPONSE_INVALID, operation=operation)
        return data

    def _key_info(self, raw: object, *, operation: str, secrets: tuple[str, ...] = ()) -> KeyInfo:
        invalid = OpenRouterManagementError(MANAGEMENT_API_RESPONSE_INVALID, operation=operation)
        if not isinstance(raw, Mapping):
            raise invalid
        forbidden = (self._management_key, *secrets)
        hash_ = raw.get("hash")
        name = raw.get("name")
        created_at = raw.get("created_at")
        for text in (hash_, name):
            if not isinstance(text, str) or not text or len(text) > _MAX_FIELD_LENGTH:
                raise invalid
        if created_at is not None and (
            not isinstance(created_at, str) or len(created_at) > _MAX_FIELD_LENGTH
        ):
            raise invalid
        # A compromised or confused endpoint must not smuggle credentials into
        # the identity AWF persists and logs.
        if any(secret in str(value) for secret in forbidden for value in (hash_, name)):
            raise invalid
        if not _safe_hash(str(hash_)):
            raise invalid
        try:
            return KeyInfo(
                hash=str(hash_),
                name=str(name),
                disabled=bool(raw.get("disabled", False)),
                limit=_money(raw.get("limit"), nullable=True),
                limit_remaining=_money(raw.get("limit_remaining"), nullable=True, signed=True),
                usage=_money(raw.get("usage")) or Decimal(0),
                byok_usage=_money(raw.get("byok_usage", 0)) or Decimal(0),
                created_at=created_at,
            )
        except ValueError:
            raise invalid from None


def _status_reason(status: int) -> str:
    if status in {401, 403}:
        return MANAGEMENT_API_UNAUTHORIZED
    if status == 404:
        return MANAGED_KEY_NOT_FOUND
    if status == 429:
        return MANAGEMENT_API_RATE_LIMITED
    if status >= 500:
        return MANAGEMENT_API_UNAVAILABLE
    if 400 <= status < 500:
        return MANAGEMENT_API_REJECTED
    return MANAGEMENT_API_RESPONSE_INVALID


def _safe_hash(key_hash: str) -> bool:
    return bool(key_hash) and all(c.isalnum() or c in "-_" for c in key_hash)


def _path(key_hash: str) -> str:
    if not _safe_hash(key_hash):
        raise ValueError("PROVIDER_MANAGED_KEY_HASH_INVALID")
    return key_hash


def _money(value: object, *, nullable: bool = False, signed: bool = False) -> Decimal | None:
    if value is None:
        if nullable:
            return None
        raise ValueError("missing amount")
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ValueError("amount must be a number")
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("amount must be finite")
    try:
        amount = Decimal(str(value))
    except InvalidOperation:  # pragma: no cover - finite int/float always converts
        raise ValueError("amount must be a number") from None
    if amount < 0 and not signed:
        raise ValueError("amount must not be negative")
    return amount
