"""Selected-provider catalogs are bounded claims, not coding evidence."""

import httpx
import pytest

from awf.common.agent_execution import resolve_execution
from awf.service.provider_catalog import ProviderCatalog


def selection(provider="ollama", credential="KEY_A"):
    return resolve_execution(
        agent="codex",
        provider=provider,
        model="candidate",
        allow_unverified=True,
        profile={"runtime": {"environment": {"OPENROUTER_API_KEY": "${" + credential + "}"}}},
    )


@pytest.mark.asyncio
async def test_catalog_selected_only_cached_and_scoped():
    calls = []

    def respond(request):
        calls.append(request)
        return httpx.Response(
            200,
            json={
                "data": [
                    {"id": "candidate", "supported_parameters": ["tools"], "context_length": 4000}
                ]
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        catalog = ProviderCatalog(client=client)
        first = await catalog.query(selection("openrouter"), environ={"KEY_A": "secret-a"})
        assert first.models[0].tools is True
        assert first.models[0].pricing is None
        assert first.evidence == "provider_claim"
        assert not first.coding_compatible
        assert (await catalog.query(selection("openrouter"), environ={"KEY_A": "secret-a"})).cached
        await catalog.query(selection("openrouter", "KEY_B"), environ={"KEY_B": "secret-b"})
    assert len(calls) == 2
    assert all(str(r.url) == "https://openrouter.ai/api/v1/models" for r in calls)
    assert "secret" not in repr(catalog.cache_keys)
    assert "secret" not in first.model_dump_json()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status,body",
    [
        (401, {}),
        (429, {}),
        (302, {}),
        (200, {"models": "bad"}),
        (200, {"models": [{"name": "x" * 5000}]}),
    ],
)
async def test_unavailable_keeps_manual_entry_and_hides_body(status, body):
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(status, json=body))
    ) as client:
        result = await ProviderCatalog(client=client).query(selection(), environ={})
    assert result.status == "unavailable"
    assert result.manual_entry_allowed
    assert result.models == []


@pytest.mark.asyncio
async def test_sidecar_requires_network_transport_and_native_no_catalog():
    sidecar = selection().model_copy(update={"network_service": "ollama"})
    catalog = ProviderCatalog()
    assert (
        await catalog.query(sidecar, environ={})
    ).reason_code == "PROVIDER_DISCOVERY_NETWORK_UNSUPPORTED"
    native = resolve_execution(agent="codex")
    assert (await catalog.query(native, environ={})).status == "unsupported"


@pytest.mark.asyncio
async def test_catalog_concurrent_misses_expiry_and_item_bounds(monkeypatch):
    import asyncio

    import awf.service.provider_catalog as module

    now = [100.0]
    monkeypatch.setattr(module, "monotonic", lambda: now[0])
    calls = []

    async def respond(request):
        calls.append(request)
        await asyncio.sleep(0)
        return httpx.Response(
            200, json={"models": [{"name": "candidate:cloud", "digest": "revision"}]}
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        catalog = ProviderCatalog(client=client)
        results = await asyncio.gather(*(catalog.query(selection(), environ={}) for _ in range(3)))
        assert len(calls) == 1
        assert results[0].models[0].version == "revision"
        now[0] += 301
        await catalog.query(selection(), environ={})
        assert len(calls) == 2
        monkeypatch.setattr(module, "MAX_MODELS", 0)
        now[0] += 301
        assert (await catalog.query(selection(), environ={})).status == "unavailable"


@pytest.mark.asyncio
async def test_response_size_and_transport_failure_are_bounded(monkeypatch):
    import awf.service.provider_catalog as module

    monkeypatch.setattr(module, "MAX_RESPONSE_BYTES", 20)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, content=b"x" * 21))
    ) as client:
        assert (
            await ProviderCatalog(client=client).query(selection(), environ={})
        ).status == "unavailable"

    def failed(_request):
        raise httpx.ReadTimeout("private-provider-detail")

    async with httpx.AsyncClient(transport=httpx.MockTransport(failed)) as client:
        result = await ProviderCatalog(client=client).query(selection(), environ={})
        assert "private-provider-detail" not in result.model_dump_json()


@pytest.mark.asyncio
async def test_catalog_cancellation_does_not_cache_or_replay():
    import asyncio

    started = asyncio.Event()

    async def pending(_request):
        started.set()
        await asyncio.Event().wait()

    async with httpx.AsyncClient(transport=httpx.MockTransport(pending)) as client:
        catalog = ProviderCatalog(client=client)
        task = asyncio.create_task(catalog.query(selection(), environ={}))
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not catalog.cache_keys
