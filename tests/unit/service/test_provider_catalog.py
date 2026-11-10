"""Selected-provider catalogs are bounded claims, not coding evidence."""

import asyncio

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


@pytest.mark.asyncio
@pytest.mark.parametrize("cached", [False, True])
async def test_slow_miss_does_not_block_an_independent_key(cached):
    started = asyncio.Event()
    release = asyncio.Event()
    calls = []

    async def respond(request):
        calls.append(request)
        if request.headers["Authorization"] == "Bearer slow":
            started.set()
            await release.wait()
        return httpx.Response(200, json={"data": [{"id": "candidate"}]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        catalog = ProviderCatalog(client=client)
        fast = selection("openrouter", "FAST")
        if cached:
            await catalog.query(fast, environ={"FAST": "fast"})
        slow = asyncio.create_task(
            catalog.query(selection("openrouter", "SLOW"), environ={"SLOW": "slow"})
        )
        try:
            await asyncio.wait_for(started.wait(), 1)
            result = await asyncio.wait_for(catalog.query(fast, environ={"FAST": "fast"}), 0.3)
            assert result.status == "available"
            assert result.cached is cached
            assert not slow.done()
        finally:
            release.set()
            await slow
        assert len(calls) == 2


@pytest.mark.asyncio
async def test_same_key_wait_obeys_its_total_deadline(monkeypatch):
    import awf.service.provider_catalog as module

    started = asyncio.Event()
    release = asyncio.Event()
    calls = []

    async def respond(request):
        calls.append(request)
        started.set()
        await release.wait()
        return httpx.Response(200, json={"models": []})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        catalog = ProviderCatalog(client=client)
        leader = asyncio.create_task(catalog.query(selection(), environ={}))
        try:
            await asyncio.wait_for(started.wait(), 1)
            monkeypatch.setattr(module, "CATALOG_TIMEOUT_SECONDS", 0.02, raising=False)
            follower = await asyncio.wait_for(catalog.query(selection(), environ={}), 0.3)
            assert follower.status == "unavailable"
            assert not leader.done()
            assert not catalog.cache_keys
        finally:
            release.set()
            assert (await leader).status == "available"
        assert (await catalog.query(selection(), environ={})).cached
        assert len(calls) == 1


@pytest.mark.asyncio
async def test_http_timeout_releases_deduplication_without_caching(monkeypatch):
    import awf.service.provider_catalog as module

    monkeypatch.setattr(module, "CATALOG_TIMEOUT_SECONDS", 0.02, raising=False)
    calls = []

    async def respond(request):
        calls.append(request)
        if len(calls) == 1:
            await asyncio.Event().wait()
        return httpx.Response(200, json={"models": []})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        catalog = ProviderCatalog(client=client)
        result = await asyncio.wait_for(catalog.query(selection(), environ={}), 0.3)
        assert result.status == "unavailable"
        assert not catalog.cache_keys
        assert (await catalog.query(selection(), environ={})).status == "available"
        assert len(calls) == 2


@pytest.mark.asyncio
async def test_pending_key_bookkeeping_is_bounded(monkeypatch):
    import awf.service.provider_catalog as module

    monkeypatch.setattr(module, "MAX_INFLIGHT_ENTRIES", 1, raising=False)
    started = asyncio.Event()
    release = asyncio.Event()
    calls = []

    async def respond(request):
        calls.append(request)
        started.set()
        await release.wait()
        return httpx.Response(200, json={"models": []})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        catalog = ProviderCatalog(client=client)
        leader = asyncio.create_task(catalog.query(selection(), environ={}, context="first"))
        try:
            await asyncio.wait_for(started.wait(), 1)
            result = await asyncio.wait_for(
                catalog.query(selection(), environ={}, context="second"), 0.3
            )
            assert result.status == "unavailable"
            assert len(calls) == 1
        finally:
            release.set()
            await leader
        assert (
            await catalog.query(selection(), environ={}, context="second")
        ).status == "available"
        assert len(calls) == 2


@pytest.mark.asyncio
async def test_cancelling_a_follower_does_not_cancel_the_shared_request():
    started = asyncio.Event()
    release = asyncio.Event()
    calls = []

    async def respond(request):
        calls.append(request)
        started.set()
        await release.wait()
        return httpx.Response(200, json={"models": []})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        catalog = ProviderCatalog(client=client)
        leader = asyncio.create_task(catalog.query(selection(), environ={}))
        try:
            await asyncio.wait_for(started.wait(), 1)
            follower = asyncio.create_task(catalog.query(selection(), environ={}))
            await asyncio.sleep(0)
            follower.cancel()
            with pytest.raises(asyncio.CancelledError):
                await follower
            assert not leader.done()
        finally:
            release.set()
            assert (await leader).status == "available"
        assert (await catalog.query(selection(), environ={})).cached
        assert len(calls) == 1


@pytest.mark.asyncio
async def test_cancelling_a_leader_releases_followers_without_replay():
    started = asyncio.Event()
    calls = []

    async def respond(request):
        calls.append(request)
        started.set()
        await asyncio.Event().wait()

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        catalog = ProviderCatalog(client=client)
        leader = asyncio.create_task(catalog.query(selection(), environ={}))
        await asyncio.wait_for(started.wait(), 1)
        follower = asyncio.create_task(catalog.query(selection(), environ={}))
        await asyncio.sleep(0)
        leader.cancel()
        with pytest.raises(asyncio.CancelledError):
            await leader
        result = await asyncio.wait_for(follower, 0.3)
        assert result.status == "unavailable"
        assert not result.cached
        assert not catalog.cache_keys
        assert len(calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("body", [[], {}, {"models": [None]}, {"models": [{"name": ""}]}])
async def test_invalid_catalog_shapes_fail_closed_without_caching(body):
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=body))
    ) as client:
        catalog = ProviderCatalog(client=client)
        result = await catalog.query(selection(), environ={})
        assert result.status == "unavailable"
        assert result.models == []
        assert not catalog.cache_keys


@pytest.mark.asyncio
async def test_missing_credential_never_contacts_the_provider():
    calls = []

    def respond(request):
        calls.append(request)
        return httpx.Response(200, json={"data": []})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        result = await ProviderCatalog(client=client).query(selection("openrouter"), environ={})
        assert result.status == "unavailable"
        assert not calls


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["id", "digest", "pricing"])
async def test_provider_cannot_echo_credentials_in_catalog_claims(field):
    item = {"id": "candidate", field: "synthetic-private-value"}
    if field == "pricing":
        item[field] = {"prompt": "synthetic-private-value"}
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json={"data": [item]}))
    ) as client:
        catalog = ProviderCatalog(client=client)
        result = await catalog.query(
            selection("openrouter"), environ={"KEY_A": "synthetic-private-value"}
        )
        assert result.status == "unavailable"
        assert "synthetic-private-value" not in result.model_dump_json()
        assert not catalog.cache_keys


@pytest.mark.asyncio
async def test_catalog_normalizes_optional_claims_and_isolates_cached_values():
    items = [
        {
            "id": "candidate",
            "digest": "x" * 500,
            "context_length": True,
            "capabilities": ["completion"],
            "pricing": {"prompt": "1" * 101, "completion": 2, "ignored": "drop"},
        },
        {"id": "minimal", "context_length": -1},
    ]
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json={"data": items}))
    ) as client:
        catalog = ProviderCatalog(client=client)
        first = await catalog.query(selection("openrouter"), environ={"KEY_A": "private"})
        assert first.models[0].version is None
        assert first.models[0].context_length is None
        assert first.models[0].tools is False
        assert first.models[0].pricing == {"prompt": "1" * 100, "completion": "2"}
        assert first.models[1].tools is None
        first.models.clear()
        hit = await catalog.query(selection("openrouter"), environ={"KEY_A": "private"})
        assert hit.cached and len(hit.models) == 2
        hit.models.clear()
        assert len((await catalog.query(selection("openrouter"), environ={})).models) == 2


@pytest.mark.asyncio
async def test_catalog_evicts_the_least_recently_used_key(monkeypatch):
    import awf.service.provider_catalog as module

    monkeypatch.setattr(module, "MAX_CACHE_ENTRIES", 2)
    calls = []

    def respond(request):
        calls.append(request)
        return httpx.Response(200, json={"models": []})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        catalog = ProviderCatalog(client=client)
        for context in ["a", "b", "a", "c"]:
            await catalog.query(selection(), environ={}, context=context)
        assert [key[-1] for key in catalog.cache_keys] == ["a", "c"]
        assert len(calls) == 3
        assert not (await catalog.query(selection(), environ={}, context="b")).cached
        assert len(calls) == 4


@pytest.mark.asyncio
async def test_catalog_owned_client_disables_proxy_environment_and_redirects(monkeypatch):
    import awf.service.provider_catalog as module

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json={"models": []}))
    )
    options = []

    def make_client(**kwargs):
        options.append(kwargs)
        return client

    monkeypatch.setattr(module.httpx, "AsyncClient", make_client)
    assert (await ProviderCatalog().query(selection(), environ={})).status == "available"
    assert options == [{"timeout": 10, "follow_redirects": False, "trust_env": False}]
    assert client.is_closed
