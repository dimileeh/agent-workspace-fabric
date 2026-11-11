"""In-memory fake of OpenRouter's key-management API (no live calls)."""

from __future__ import annotations

import json
from typing import Any

import httpx

from awf.service.openrouter_management import OpenRouterManagementClient

FAKE_MANAGEMENT_KEY = "sk-or-mgmt-fake-sentinel-9f8e7d6c5b4a"


class FakeOpenRouterManagement:
    """Records every call; ``fail`` maps ``(method, kind)`` to an HTTP status."""

    def __init__(self) -> None:
        self.keys: dict[str, dict[str, Any]] = {}
        self.values: dict[str, str] = {}
        self.calls: list[tuple[str, str, dict[str, Any] | None]] = []
        self.fail: dict[tuple[str, str], int] = {}
        self._counter = 0

    @property
    def minted_values(self) -> list[str]:
        return list(self.values.values())

    def client(self, management_key: str = FAKE_MANAGEMENT_KEY) -> OpenRouterManagementClient:
        return OpenRouterManagementClient(
            management_key, transport=httpx.MockTransport(self.handler)
        )

    def set_usage(self, key_hash: str, usage: float, byok: float = 0.0) -> None:
        self.keys[key_hash]["usage"] = usage
        self.keys[key_hash]["byok_usage"] = byok

    def by_name(self, name: str) -> dict[str, Any] | None:
        return next((key for key in self.keys.values() if key["name"] == name), None)

    def add_key(self, name: str, *, limit: float = 1.0, usage: float = 0.0) -> str:
        self._counter += 1
        key_hash = f"hash{self._counter}"
        self.keys[key_hash] = {
            "hash": key_hash,
            "name": name,
            "disabled": False,
            "limit": limit,
            "usage": usage,
            "byok_usage": 0.0,
            "include_byok_in_limit": True,
            "created_at": "2026-11-10T00:00:00Z",
        }
        self.values[key_hash] = f"sk-or-v1-fake-minted-{self._counter:04d}-c0ffee"
        return key_hash

    def handler(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        path = request.url.path.removeprefix("/api/v1")
        kind = "collection" if path == "/keys" else "key"
        self.calls.append((request.method, path, body))
        status = self.fail.get((request.method, kind))
        if status is not None:
            return httpx.Response(status, json={"error": {"message": "injected"}})
        if request.method == "POST" and kind == "collection":
            assert body is not None and body["include_byok_in_limit"] is True
            key_hash = self.add_key(body["name"], limit=body["limit"])
            return httpx.Response(
                201, json={"data": self._view(key_hash), "key": self.values[key_hash]}
            )
        if request.method == "GET" and kind == "collection":
            offset = int(request.url.params.get("offset", "0"))
            page = list(self.keys)[offset : offset + 100]
            return httpx.Response(200, json={"data": [self._view(h) for h in page]})
        key_hash = path.rsplit("/", 1)[-1]
        if key_hash not in self.keys:
            return httpx.Response(404, json={"error": {"message": "not found"}})
        if request.method == "DELETE":
            del self.keys[key_hash]
            return httpx.Response(200, json={"deleted": True})
        if request.method == "PATCH":
            assert body is not None
            self.keys[key_hash]["limit"] = body["limit"]
        return httpx.Response(200, json={"data": self._view(key_hash)})

    def _view(self, key_hash: str) -> dict[str, Any]:
        key = dict(self.keys[key_hash])
        limit = key["limit"]
        key["limit_remaining"] = None if limit is None else limit - key["usage"] - key["byok_usage"]
        return key
