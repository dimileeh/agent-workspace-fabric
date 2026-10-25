"""Stream boundaries cannot expose exact or token-shaped credential fragments."""

import pytest

from awf.hosted_node.runtime_consumers import _Output


@pytest.mark.parametrize("exact", [False, True])
@pytest.mark.parametrize("split", [1, 7, 23, 38])
async def test_stream_redacts_raw_secret_across_chunks(exact, split):
    secret = "sk-proj-" + "A" * 16 + "B" * 16
    emitted = []
    output = _Output(emitted.append, (secret,) if exact else ("unrelated-key",))
    await output.write("before " + secret[:split])
    await output.write(secret[split:] + " after\n")
    await output.flush()
    assert "".join(emitted) == "before <redacted> after\n"


@pytest.mark.parametrize(
    "secret",
    ["plain-scoped-credential", "ghp_" + "A" * 32, "Bearer " + "B" * 32],
)
async def test_stream_retains_matching_context_for_every_single_character_chunk(secret):
    emitted = []
    exact = (secret,) if secret.startswith("plain") else ()
    output = _Output(emitted.append, exact)
    for char in "before " + secret + " after\n":
        await output.write(char)
    await output.flush()
    safe = "".join(emitted)
    assert "before " in safe and safe.endswith(" after\n")
    assert "<redacted>" in safe
    assert "credential" not in safe and "AAAA" not in safe and "BBBB" not in safe


async def test_stream_is_bounded_for_unterminated_generic_token_and_resumes_safe_output():
    emitted = []

    async def receive(value):
        emitted.append(value)

    output = _Output(receive, ())
    await output.write("sk-proj-")
    for _ in range(128):
        await output.write("A" * 1024)
        assert len(output.pending) <= 65536
    await output.write("\nnext safe line\n")
    await output.flush()
    safe = "".join(emitted)
    assert "AAAA" not in safe
    assert safe.endswith("next safe line\n")
    assert "<redacted>" in safe


async def test_stream_flushes_safe_partial_text_and_both_callback_styles():
    emitted = []
    output = _Output(emitted.append, ("never-printed",))
    await output.write("ordinary ")
    await output.write("partial output")
    await output.flush()
    await output.flush()
    assert "".join(emitted) == "ordinary partial output"
    assert output.pending == ""


async def test_stream_eof_suppresses_incomplete_exact_secret():
    emitted = []
    output = _Output(emitted.append, ("secret-first\nsecret-second",))
    await output.write("before secret-first\nsecret-")
    await output.flush()
    assert "".join(emitted) == "before <redacted>"
    assert output.pending == ""


async def test_stream_oversized_multiline_secret_stays_closed_after_newlines():
    emitted = []
    output = _Output(emitted.append, ())
    await output.write("PRIVATE_KEY=-----BEGIN PRIVATE KEY-----\n")
    for _ in range(70):
        await output.write("A" * 1024 + "\n")
        assert len(output.pending) <= 65536
    await output.write("sensitive-tail\n-----END PRIVATE KEY-----\n")
    await output.flush()
    assert "".join(emitted) == "<redacted>"


@pytest.mark.parametrize(
    ("text", "secrets"),
    [
        ("before exact-first\nexact-second after\n", ("exact-first\nexact-second",)),
        (
            "PRIVATE_KEY=-----BEGIN PRIVATE KEY-----\nsecret-body\n-----END PRIVATE KEY-----\nsafe\n",
            (),
        ),
    ],
)
async def test_stream_keeps_multiline_matching_context(text, secrets):
    emitted = []
    output = _Output(emitted.append, secrets)
    for line in text.splitlines(keepends=True):
        await output.write(line)
    await output.flush()
    safe = "".join(emitted)
    assert "<redacted>" in safe
    assert "secret-body" not in safe and "exact-first" not in safe and "exact-second" not in safe


async def test_runner_redacts_both_streams_when_delegate_fails(tmp_path, monkeypatch):
    from unittest.mock import AsyncMock

    from awf.hosted_node.runtime_consumers import HostedCommandRunner
    from tests.unit.test_hosted_runtime_consumers import consumers

    scope, _ = consumers(tmp_path, monkeypatch)
    emitted = []

    async def run_streaming(*args, on_stdout, on_stderr, **kwargs):
        for receive in (on_stdout, on_stderr):
            await receive("sk-proj-" + "A" * 16)
            await receive("B" * 16)
        raise OSError("synthetic failure")

    delegate = AsyncMock(run_streaming=AsyncMock(side_effect=run_streaming))
    with pytest.raises(OSError):
        async with scope.scope("ws_exact"):
            await HostedCommandRunner(scope, delegate).run_streaming(
                ["gh", "api"], on_stdout=emitted.append, on_stderr=emitted.append
            )
    assert "".join(emitted) == "<redacted><redacted>"
