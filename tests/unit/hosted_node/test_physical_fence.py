import asyncio
import inspect
import json
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import httpx
import pytest

from awf.hosted_node.host_config import PhysicalFenceAuthoritySettings
from awf.hosted_node.host_runtime import PhysicalFenceEvidence
from awf.hosted_node.physical_fence import PhysicalFenceVerifier, physical_fence_client
from awf.hosted_node.schemas import Identity
from tests.unit.hosted_node.test_physical_fence_config import AUTHORITY

OLD = Identity(owner="org/node", incarnation="100", recovery_generation=1, boot_epoch=1)
NEW = Identity(owner="org/node", incarnation="200", recovery_generation=2, boot_epoch=2)
RECEIPT = str(uuid4())


def response_body(request, **changes):
    return {
        **json.loads(request.content),
        "expires_at": (datetime.now(UTC) + timedelta(seconds=20)).isoformat(),
        **changes,
    }


async def lookup(handler, old=OLD, new=NEW, receipt=RECEIPT):
    async def streaming_handler(request):
        result = handler(request)
        if inspect.isawaitable(result):
            result = await result
        return httpx.Response(
            result.status_code, headers=result.headers, stream=httpx.ByteStream(result.content)
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(streaming_handler)) as client:
        verifier = PhysicalFenceVerifier(
            PhysicalFenceAuthoritySettings(**AUTHORITY), OLD.owner, client
        )
        return await verifier.verify(old, new, receipt)


async def test_exact_wire_contract_and_fresh_challenge_on_every_lookup():
    challenges = []

    def handler(request):
        assert request.method == "POST" and str(request.url) == AUTHORITY["url"]
        assert request.headers["Authorization"] == "Bearer " + AUTHORITY["bearer"]
        body = json.loads(request.content)
        assert set(body) == {"previous_identity", "identity", "receipt_ref", "challenge"}
        assert body["previous_identity"] == OLD.model_dump()
        assert body["identity"] == NEW.model_dump()
        assert body["receipt_ref"] == RECEIPT
        nonce = UUID(body["challenge"])
        assert nonce.version == 4 and str(nonce) == body["challenge"]
        challenges.append(nonce)
        return httpx.Response(200, json=response_body(request))

    for _ in range(2):
        evidence = await lookup(handler)
        assert isinstance(evidence, PhysicalFenceEvidence)
        assert evidence.previous_identity == OLD and evidence.identity == NEW
        assert evidence.receipt_ref == RECEIPT
    assert len(set(challenges)) == 2


@pytest.mark.parametrize("which", ["previous_identity", "identity"])
@pytest.mark.parametrize(
    "field,value",
    [
        ("owner", "other/node"),
        ("incarnation", "999"),
        ("recovery_generation", 3),
        ("boot_epoch", 3),
    ],
)
async def test_every_response_identity_component_must_match(which, field, value):
    def handler(request):
        body = response_body(request)
        body[which][field] = value
        return httpx.Response(200, json=body)

    assert await lookup(handler) is None


@pytest.mark.parametrize(
    "changes",
    [
        {"receipt_ref": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"},
        {"challenge": str(uuid4())},
        {"approved": True},
        {"identity": None},
        {"previous_identity": {**OLD.model_dump(), "extra": True}},
        {"identity": {**NEW.model_dump(), "boot_epoch": "2"}},
        {"identity": {**NEW.model_dump(), "recovery_generation": True}},
        {"receipt_ref": 123},
        {"receipt_ref": "x" * 129},
        {"challenge": 123},
        {"expires_at": "2026-01-01T00:00:00"},
        {"expires_at": "not-a-date"},
        {"expires_at": 9999999999},
        {"expires_at": None},
    ],
)
async def test_response_is_strict_and_exact(changes):
    assert (
        await lookup(lambda request: httpx.Response(200, json=response_body(request, **changes)))
        is None
    )


@pytest.mark.parametrize(
    "field", ["identity", "previous_identity", "receipt_ref", "challenge", "expires_at"]
)
async def test_missing_response_field_denies(field):
    def handler(request):
        body = response_body(request)
        del body[field]
        return httpx.Response(200, json=body)

    assert await lookup(handler) is None


@pytest.mark.parametrize(
    "seconds,accepted", [(-1, False), (0, False), (0.001, True), (30, True), (30.001, False)]
)
async def test_expiry_window_is_open_at_now_closed_at_thirty_seconds(
    monkeypatch, seconds, accepted
):
    from awf.hosted_node import physical_fence

    now = datetime.now(UTC)

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            assert tz == UTC
            return now

    monkeypatch.setattr(physical_fence, "datetime", Clock)
    evidence = await lookup(
        lambda request: httpx.Response(
            200,
            json=response_body(request, expires_at=(now + timedelta(seconds=seconds)).isoformat()),
        )
    )
    assert (evidence is not None) is accepted


@pytest.mark.parametrize(
    "status", [201, 204, 301, 302, 303, 307, 308, 400, 401, 403, 404, 429, 500, 503]
)
async def test_only_200_is_evidence_and_redirects_are_not_followed(status):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(
            status, json=response_body(request), headers={"Location": "https://other.example/"}
        )

    assert await lookup(handler) is None
    assert len(calls) == 1


@pytest.mark.parametrize("body", [b"not-json", b"[]", b"null", b"{}", b"\xff", b"{" * 8192])
async def test_malformed_response_denies(body):
    assert (
        await lookup(
            lambda _: httpx.Response(
                200, content=body, headers={"content-type": "application/json"}
            )
        )
        is None
    )


@pytest.mark.parametrize("which", ["old", "new", "both"])
async def test_request_owner_must_be_the_server_configured_node(which):
    def unexpected(_):
        pytest.fail("cross-owner lookup must not leave the process")

    old = OLD.model_copy(update={"owner": "other/node"}) if which in {"old", "both"} else OLD
    new = NEW.model_copy(update={"owner": "other/node"}) if which in {"new", "both"} else NEW
    assert await lookup(unexpected, old, new) is None


@pytest.mark.parametrize(
    "receipt",
    [
        "",
        "x" * 129,
        "https://request.example/fence",
        "../approval.json",
        "not-a-reservation-uuid",
        "00000000-0000-0000-0000-000000000000",
        "AAAAAAAA-AAAA-4AAA-8AAA-AAAAAAAAAAAA",
        "aaaaaaaaaaaa4aaa8aaaaaaaaaaaaaaa",
    ],
)
async def test_invalid_receipt_never_becomes_a_destination(receipt):
    def unexpected(_):
        pytest.fail("invalid reference must not leave the process")

    assert await lookup(unexpected, receipt=receipt) is None


async def test_authority_exception_is_sanitized_and_cancellation_propagates(capsys, caplog):
    async def failure(_):
        raise httpx.ConnectError("sensitive " + AUTHORITY["bearer"])

    assert await lookup(failure) is None
    assert AUTHORITY["bearer"] not in capsys.readouterr().out + caplog.text

    async def cancelled(_):
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await lookup(cancelled)


async def test_client_has_verified_tls_no_proxy_no_redirect_and_bounded_pool(monkeypatch):
    import ssl

    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("SSL_CERT_FILE", "/untrusted/environment/certificate")
    async with physical_fence_client() as client:
        assert not client.follow_redirects and not client.trust_env
        assert client.timeout == httpx.Timeout(2)
        assert client._transport._pool._ssl_context.verify_mode == ssl.CERT_REQUIRED
        assert client._transport._pool._ssl_context.check_hostname
        assert client._transport._pool._max_connections == 1
        assert not client._mounts
    assert client.is_closed
