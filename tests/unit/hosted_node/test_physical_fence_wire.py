"""Real loopback TLS/HTTP; no provider, shared database or container fixtures."""

import asyncio
import json
import ssl
import subprocess
from contextlib import asynccontextmanager, suppress
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from awf.hosted_node.host_config import PhysicalFenceAuthoritySettings
from awf.hosted_node.physical_fence import PhysicalFenceVerifier, physical_fence_client
from tests.unit.hosted_node.test_physical_fence import NEW, OLD, RECEIPT
from tests.unit.hosted_node.test_physical_fence_config import AUTHORITY


@pytest.fixture(scope="module")
def tls_contexts(tmp_path_factory):
    root = tmp_path_factory.mktemp("physical-fence-tls")
    key, cert = root / "synthetic.key", root / "synthetic.pem"
    subprocess.run(
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-days",
            "1",
            "-subj",
            "/CN=localhost",
            "-addext",
            "subjectAltName=DNS:localhost,IP:127.0.0.1",
            "-keyout",
            str(key),
            "-out",
            str(cert),
        ],
        check=True,
        capture_output=True,
    )
    server = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server.load_cert_chain(cert, key)
    client = ssl.create_default_context(cafile=str(cert))
    return server, client


@pytest.fixture
def wire(tls_contexts):
    @asynccontextmanager
    async def serve(reply):
        requests, tasks = [], set()

        async def handle(reader, writer):
            task = asyncio.current_task()
            tasks.add(task)
            try:
                head = await reader.readuntil(b"\r\n\r\n")
                first, *lines = head.decode("ascii").strip().split("\r\n")
                headers = dict(line.split(": ", 1) for line in lines)
                body = json.loads(await reader.readexactly(int(headers["Content-Length"])))
                requests.append((first, headers, body))
                await reply(writer, body)
            except (ConnectionError, asyncio.IncompleteReadError):
                pass
            finally:
                writer.close()
                with suppress(ConnectionError):
                    await writer.wait_closed()
                tasks.discard(task)

        server = await asyncio.start_server(handle, "127.0.0.1", 0, ssl=tls_contexts[0])
        port = server.sockets[0].getsockname()[1]
        try:
            yield (
                f"https://127.0.0.1:{port}/internal/v1/vm-nodes/org/node/physical-fence",
                requests,
                tls_contexts[1],
            )
        finally:
            server.close()
            await server.wait_closed()
            pending = tuple(tasks)
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)

    return serve


def proof(body):
    return {**body, "expires_at": (datetime.now(UTC) + timedelta(seconds=20)).isoformat()}


async def respond(writer, body, *, status=200, headers=None, raw=None):
    raw = json.dumps(proof(body)).encode() if raw is None else raw
    values = {
        "Content-Type": "application/json",
        "Content-Length": str(len(raw)),
        "Connection": "close",
        **(headers or {}),
    }
    head = f"HTTP/1.1 {status} Test\r\n" + "".join(f"{k}: {v}\r\n" for k, v in values.items())
    writer.write(head.encode() + b"\r\n" + raw)
    await writer.drain()


def verifier(url, client):
    return PhysicalFenceVerifier(
        PhysicalFenceAuthoritySettings(**{**AUTHORITY, "url": url}), OLD.owner, client
    )


async def test_real_tls_post_and_secret_not_in_body(wire, monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:1")
    async with (
        wire(respond) as (url, requests, context),
        httpx.AsyncClient(verify=context, trust_env=False) as client,
    ):
        evidence = await verifier(url, client).verify(OLD, NEW, RECEIPT)
    assert evidence and evidence.previous_identity == OLD and evidence.identity == NEW
    method, headers, body = requests[0]
    assert method == "POST /internal/v1/vm-nodes/org/node/physical-fence HTTP/1.1"
    assert headers["Authorization"] == "Bearer " + AUTHORITY["bearer"]
    assert headers["Accept-Encoding"] == "identity"
    assert AUTHORITY["bearer"] not in json.dumps(body)
    assert set(body) == {"previous_identity", "identity", "receipt_ref", "challenge"}


@pytest.mark.parametrize(
    "case",
    [
        "old",
        "new",
        "owner",
        "receipt",
        "nonce",
        "extra",
        "nested-extra",
        "missing",
        "naive",
        "expired",
        "future",
        "numeric-expiry",
        "string-generation",
        "malformed",
        "duplicate",
        "html",
        "gzip",
        "large-length",
        "bad-length",
        "negative-length",
        "truncated",
        "redirect",
        "unauthorized",
        "provider-error",
    ],
)
async def test_real_wire_negative_contract(wire, case, capsys, caplog):
    async def reply(writer, body):
        result = proof(body)
        status, headers = 200, {}
        if case == "old":
            result["previous_identity"]["incarnation"] = "999"
        elif case == "new":
            result["identity"]["boot_epoch"] += 1
        elif case == "owner":
            result["identity"]["owner"] = "other/node"
        elif case == "receipt":
            result["receipt_ref"] = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
        elif case == "nonce":
            result["challenge"] = "00000000-0000-4000-8000-000000000000"
        elif case == "extra":
            result["provider_evidence"] = AUTHORITY["bearer"]
        elif case == "nested-extra":
            result["identity"]["approved"] = True
        elif case == "missing":
            del result["previous_identity"]
        elif case == "naive":
            result["expires_at"] = "2099-01-01T00:00:00"
        elif case == "expired":
            result["expires_at"] = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
        elif case == "future":
            result["expires_at"] = (datetime.now(UTC) + timedelta(seconds=31)).isoformat()
        elif case == "numeric-expiry":
            result["expires_at"] = (datetime.now(UTC) + timedelta(seconds=20)).timestamp()
        elif case == "string-generation":
            result["identity"]["recovery_generation"] = "2"
        elif case == "html":
            headers["Content-Type"] = "text/html"
        elif case == "gzip":
            headers["Content-Encoding"] = "gzip"
        elif case == "large-length":
            headers["Content-Length"] = "8193"
        elif case == "bad-length":
            headers["Content-Length"] = "not-a-length"
        elif case == "negative-length":
            headers["Content-Length"] = "-1"
        elif case == "truncated":
            headers["Content-Length"] = "8192"
        elif case == "redirect":
            status, headers["Location"] = 307, "/other"
        elif case == "unauthorized":
            status = 401
        elif case == "provider-error":
            status = 503
        raw = json.dumps(result).encode()
        if case == "malformed":
            raw = b"not json " + AUTHORITY["bearer"].encode()
        elif case == "duplicate":
            raw = b'{"receipt_ref":"conflicting",' + raw[1:]
        await respond(writer, body, status=status, headers=headers, raw=raw)

    async with wire(reply) as (url, requests, context):
        async with httpx.AsyncClient(
            verify=context, follow_redirects=True, trust_env=False
        ) as client:
            assert await verifier(url, client).verify(OLD, NEW, RECEIPT) is None
        assert len(requests) == 1
    captured = capsys.readouterr()
    assert AUTHORITY["bearer"] not in captured.out + captured.err + caplog.text


@pytest.mark.parametrize("size", [8192, 8193])
@pytest.mark.parametrize("chunked", [False, True])
async def test_wire_size_limit_with_and_without_content_length(wire, size, chunked):
    async def reply(writer, body):
        raw = json.dumps(proof(body)).encode()
        raw += b" " * (size - len(raw))
        if not chunked:
            await respond(writer, body, raw=raw)
            return
        writer.write(
            b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nTransfer-Encoding: chunked\r\n\r\n"
        )
        for offset in range(0, len(raw), 127):
            part = raw[offset : offset + 127]
            writer.write(f"{len(part):x}\r\n".encode() + part + b"\r\n")
            await writer.drain()
        writer.write(b"0\r\n\r\n")
        await writer.drain()

    async with (
        wire(reply) as (url, _, context),
        httpx.AsyncClient(verify=context, trust_env=False) as client,
    ):
        evidence = await verifier(url, client).verify(OLD, NEW, RECEIPT)
        assert (evidence is not None) is (size == 8192)


@pytest.mark.parametrize("drip", [False, True])
async def test_total_deadline_covers_headers_and_trickling_body(wire, drip):
    async def reply(writer, body):
        if not drip:
            await asyncio.sleep(5)
            await respond(writer, body)
            return
        writer.write(
            b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nTransfer-Encoding: chunked\r\n\r\n"
        )
        while True:
            writer.write(b"1\r\n \r\n")
            await writer.drain()
            await asyncio.sleep(0.1)

    async with (
        wire(reply) as (url, _, context),
        httpx.AsyncClient(verify=context, trust_env=False, timeout=10) as client,
    ):
        started = asyncio.get_running_loop().time()
        assert await verifier(url, client).verify(OLD, NEW, RECEIPT) is None
        assert 1.8 <= asyncio.get_running_loop().time() - started < 2.8
        assert not client._transport._pool.connections


async def test_cancellation_closes_wire_and_propagates(wire):
    received, closed = asyncio.Event(), asyncio.Event()

    async def reply(writer, body):
        received.set()
        try:
            await asyncio.sleep(10)
        finally:
            closed.set()

    async with (
        wire(reply) as (url, _, context),
        httpx.AsyncClient(verify=context, trust_env=False) as client,
    ):
        task = asyncio.create_task(verifier(url, client).verify(OLD, NEW, RECEIPT))
        await asyncio.wait_for(received.wait(), 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not client._transport._pool.connections
    assert closed.is_set()


async def test_production_client_rejects_untrusted_tls_before_bearer_sent(wire):
    async with wire(respond) as (url, requests, _):
        async with physical_fence_client() as client:
            assert await verifier(url, client).verify(OLD, NEW, RECEIPT) is None
        assert requests == []


async def test_old_response_cannot_be_replayed_against_fresh_challenge(wire):
    prior = None

    async def reply(writer, body):
        nonlocal prior
        if prior is None:
            prior = json.dumps(proof(body)).encode()
        await respond(writer, body, raw=prior)

    async with wire(reply) as (url, requests, context):
        async with httpx.AsyncClient(verify=context, trust_env=False) as client:
            check = verifier(url, client)
            assert await check.verify(OLD, NEW, RECEIPT) is not None
            assert await check.verify(OLD, NEW, RECEIPT) is None
        assert requests[0][2]["challenge"] != requests[1][2]["challenge"]
