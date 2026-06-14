"""Ollama URL helper and overlay-profile credential readiness checks.

Split out of ``test_provider_readiness_part_002`` to keep each part under the
first-party file line limit (see ``test_core_decomposition_maintainability``).
"""

from __future__ import annotations

import pytest

import awf.service.provider_readiness as provider_readiness
import awf.service.provider_readiness_helpers as provider_readiness_helpers


@pytest.mark.unit
def test_ollama_url_helpers_normalize_v1_and_host_gateway() -> None:
    env = {"OLLAMA_HOST": "host.docker.internal:11434/v1"}

    assert provider_readiness_helpers._ollama_version_url(env) == (
        "http://host.docker.internal:11434/api/version"
    )
    assert provider_readiness._ollama_tags_urls(env) == (
        "http://host.docker.internal:11434/api/tags",
        "http://localhost:11434/api/tags",
    )


@pytest.mark.unit
def test_ollama_url_helpers_preserve_host_gateway_fallback_port() -> None:
    env = {"AWF_OPENCODE_OLLAMA_BASE_URL": "http://host.docker.internal:23456/v1"}

    assert provider_readiness_helpers._ollama_version_urls(env) == (
        "http://host.docker.internal:23456/api/version",
        "http://localhost:23456/api/version",
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    ("env", "expected_primary"),
    [
        # A port-less host inherits Ollama's daemon port (11434) so the worker probe
        # resolves the same daemon as the OpenCode launcher prelude — not the scheme
        # default (port 80).
        ({"OLLAMA_HOST": "localhost"}, "http://localhost:11434/api/version"),
        ({"OLLAMA_HOST": "0.0.0.0"}, "http://0.0.0.0:11434/api/version"),
        ({"OLLAMA_HOST": "ollama-sidecar"}, "http://ollama-sidecar:11434/api/version"),
        (
            {"AWF_OPENCODE_OLLAMA_BASE_URL": "http://ollama.local/v1"},
            "http://ollama.local:11434/api/version",
        ),
        # A port-less IPv6 literal re-brackets and defaults the port too.
        ({"OLLAMA_HOST": "http://[::1]"}, "http://[::1]:11434/api/version"),
    ],
)
def test_ollama_url_helpers_default_portless_host_to_daemon_port(
    env: dict[str, str], expected_primary: str
) -> None:
    """A port-less ``OLLAMA_HOST`` / ``AWF_OPENCODE_OLLAMA_BASE_URL`` must resolve to
    Ollama's daemon port (11434), mirroring the OpenCode launcher prelude, so the
    worker probe/pull does not hit port 80 while the agent talks to 11434."""
    assert provider_readiness_helpers._ollama_version_urls(env) == (expected_primary,)


@pytest.mark.unit
def test_ollama_url_helpers_default_portless_host_gateway_to_daemon_port() -> None:
    """A port-less host-gateway value defaults the port on both the primary and the
    ``localhost`` fallback so launch and preflight agree on 11434."""
    env = {"OLLAMA_HOST": "host.docker.internal"}
    assert provider_readiness_helpers._ollama_version_urls(env) == (
        "http://host.docker.internal:11434/api/version",
        "http://localhost:11434/api/version",
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    "env",
    [
        # Unbalanced IPv6 brackets make ``urlsplit`` raise ``ValueError``.
        {"OLLAMA_HOST": "http://[::1"},
        {"AWF_OPENCODE_OLLAMA_BASE_URL": "http://[bad:11434"},
        # A non-numeric port makes the lazy ``.port`` accessor raise ``ValueError``
        # on a host the worker would otherwise treat as reachable (so the probe is
        # not deferred and the URL builder runs).
        {"AWF_OPENCODE_OLLAMA_BASE_URL": "http://localhost:notaport"},
        # A hostless value parses without raising but has no host to probe; it must
        # normalize to the default rather than build ``http:///api/version``.
        {"OLLAMA_HOST": "http://"},
        {"AWF_OPENCODE_OLLAMA_BASE_URL": "://"},
    ],
)
def test_ollama_url_helpers_normalize_malformed_base_url_to_default(
    env: dict[str, str],
) -> None:
    """A malformed base URL must not escape as a ``ValueError`` from the probe/pull
    URL builder during readiness. It normalizes to the ``host.docker.internal``
    default like a blank value, matching the worker reachability classifier."""
    assert provider_readiness_helpers._ollama_version_urls(env) == (
        "http://host.docker.internal:11434/api/version",
        "http://localhost:11434/api/version",
    )
    assert provider_readiness_helpers._ollama_tags_urls(env) == (
        "http://host.docker.internal:11434/api/tags",
        "http://localhost:11434/api/tags",
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    "env",
    [
        {"OLLAMA_HOST": "http://[::1"},
        {"AWF_OPENCODE_OLLAMA_BASE_URL": "http://[bad:11434"},
        {"AWF_OPENCODE_OLLAMA_BASE_URL": "http://localhost:notaport"},
        # ``urlsplit`` accepts a hostless value without raising, but an explicit URL
        # with no host can never reach a daemon, so it must be reported malformed.
        {"OLLAMA_HOST": "http://"},
        {"AWF_OPENCODE_OLLAMA_BASE_URL": "://"},
    ],
)
def test_ollama_base_url_malformed_detects_explicit_unparseable_value(
    env: dict[str, str],
) -> None:
    """An explicit ``AWF_OPENCODE_OLLAMA_BASE_URL`` / ``OLLAMA_HOST`` that cannot be
    parsed is reported as malformed so admission can fail-close, rather than being
    silently normalized to the default the URL builders fall back to."""
    assert provider_readiness_helpers._ollama_base_url_malformed(env) is True


@pytest.mark.unit
@pytest.mark.parametrize(
    "env",
    [
        # Blank/missing resolves to the always-valid default — never malformed.
        {},
        {"OLLAMA_HOST": ""},
        {"AWF_OPENCODE_OLLAMA_BASE_URL": "   "},
        # A well-formed explicit value (scheme implied or present) is not malformed.
        {"OLLAMA_HOST": "ollama-sidecar:11434"},
        {"AWF_OPENCODE_OLLAMA_BASE_URL": "http://host.docker.internal:11434/v1"},
    ],
)
def test_ollama_base_url_malformed_accepts_blank_and_valid_values(
    env: dict[str, str],
) -> None:
    assert provider_readiness_helpers._ollama_base_url_malformed(env) is False


@pytest.mark.unit
def test_overlay_profile_ollama_base_url_resolves_required_placeholder() -> None:
    """A profile may declare the Ollama endpoint via Compose's required form
    (``${OLLAMA_URL:?set OLLAMA_URL}``). When the variable is present in the worker
    environ the overlay resolves it like any other placeholder."""
    result = provider_readiness_helpers.overlay_profile_ollama_base_url(
        {"OLLAMA_URL": "http://ollama-sidecar:11434"},
        {
            "name": "ollama-sidecar",
            "runtime": {
                "environment": {
                    "AWF_OPENCODE_OLLAMA_BASE_URL": "${OLLAMA_URL:?set OLLAMA_URL}",
                }
            },
        },
    )

    assert result["AWF_OPENCODE_OLLAMA_BASE_URL"] == "http://ollama-sidecar:11434"


@pytest.mark.unit
def test_overlay_profile_ollama_base_url_treats_unset_required_placeholder_as_undeclared() -> None:
    """The required form raises ``ComposeEnvInterpolationError`` when the variable is
    absent. This overlay runs during create/retry admission before provider readiness
    — paths that only translate profile/readiness exceptions — so an unhandled raise
    would escape as a 500. The worker environ is a best-effort approximation of the
    agent's Compose context, so an unresolvable required placeholder is treated as
    undeclared (the supplied environ is returned unchanged), not surfaced as an error."""
    environ = {"OLLAMA_HOST": "http://worker-daemon:11434"}

    result = provider_readiness_helpers.overlay_profile_ollama_base_url(
        environ,
        {
            "name": "ollama-required",
            "runtime": {
                "environment": {
                    "AWF_OPENCODE_OLLAMA_BASE_URL": "${OLLAMA_URL:?set OLLAMA_URL}",
                }
            },
        },
    )

    assert result == environ
    assert "AWF_OPENCODE_OLLAMA_BASE_URL" not in result


@pytest.mark.unit
def test_overlay_profile_ollama_base_url_profile_owned_key_masks_inherited() -> None:
    """A profile-owned Ollama base-url key that resolves empty masks an inherited value.

    When the worker environ already carries ``AWF_OPENCODE_OLLAMA_BASE_URL`` and the
    profile declares the same key as a literal empty string, an unset required
    placeholder, or one resolving empty, ``runtime.environment`` owns the agent's slot:
    the OpenCode launcher keeps the empty value and the Compose merge does not inject the
    inherited worker value, so the agent falls back to ``OLLAMA_HOST`` / the default
    daemon. The overlay must drop the inherited worker value rather than leave it in the
    readiness environ — otherwise create/retry preflight and the executor probe/pull a
    daemon the agent never uses. Symmetric to
    ``overlay_profile_provider_credentials``'s profile-owned masking."""
    literal_empty = provider_readiness_helpers.overlay_profile_ollama_base_url(
        {"AWF_OPENCODE_OLLAMA_BASE_URL": "http://worker-daemon:11434"},
        {
            "name": "ollama-mask-literal-empty",
            "runtime": {"environment": {"AWF_OPENCODE_OLLAMA_BASE_URL": ""}},
        },
    )
    assert "AWF_OPENCODE_OLLAMA_BASE_URL" not in literal_empty

    required_placeholder = provider_readiness_helpers.overlay_profile_ollama_base_url(
        {"AWF_OPENCODE_OLLAMA_BASE_URL": "http://worker-daemon:11434"},
        {
            "name": "ollama-mask-required-placeholder",
            "runtime": {
                "environment": {"AWF_OPENCODE_OLLAMA_BASE_URL": "${MISSING_URL:?set MISSING_URL}"}
            },
        },
    )
    assert "AWF_OPENCODE_OLLAMA_BASE_URL" not in required_placeholder

    empty_placeholder = provider_readiness_helpers.overlay_profile_ollama_base_url(
        {"AWF_OPENCODE_OLLAMA_BASE_URL": "http://worker-daemon:11434"},
        {
            "name": "ollama-mask-empty-placeholder",
            "runtime": {"environment": {"AWF_OPENCODE_OLLAMA_BASE_URL": "${MISSING_URL}"}},
        },
    )
    assert "AWF_OPENCODE_OLLAMA_BASE_URL" not in empty_placeholder


@pytest.mark.unit
def test_overlay_profile_ollama_base_url_empty_lower_key_masks_and_shadows() -> None:
    """A profile that clears only the lower-precedence ``OLLAMA_HOST`` still owns the
    daemon selection: the Compose merge shadows the higher-precedence worker
    ``AWF_OPENCODE_OLLAMA_BASE_URL`` (``_shadowing_worker_ollama_keys``) and the agent
    sees the empty ``OLLAMA_HOST``, falling back to the default daemon. The overlay must
    drop both the inherited higher-precedence value and the owned-but-empty key so
    readiness resolves the same default daemon."""
    result = provider_readiness_helpers.overlay_profile_ollama_base_url(
        {
            "AWF_OPENCODE_OLLAMA_BASE_URL": "http://worker-daemon:11434",
            "OLLAMA_HOST": "http://worker-host:11434",
        },
        {
            "name": "ollama-clear-host",
            "runtime": {"environment": {"OLLAMA_HOST": ""}},
        },
    )
    assert "AWF_OPENCODE_OLLAMA_BASE_URL" not in result
    assert "OLLAMA_HOST" not in result


@pytest.mark.unit
def test_overlay_profile_ollama_base_url_overlays_env_secret_lease() -> None:
    """An Ollama base URL supplied through a profile ``kind="env"``/``provider="env"`` secret
    lease (e.g. ``target="OLLAMA_HOST"``, ``ref="env/HOST_OLLAMA_URL"``) reaches the agent via
    the launcher's secret-lease environment merge, not ``runtime.environment``. The overlay must
    resolve the lease's host source against ``environ`` and mask any higher-precedence inherited
    worker value — otherwise create/retry preflight and the executor auto-pull probe the
    inherited/default daemon while the agent uses the leased one. Symmetric to
    ``overlay_profile_provider_credentials``."""
    result = provider_readiness_helpers.overlay_profile_ollama_base_url(
        {
            "HOST_OLLAMA_URL": "http://ollama-sidecar:11434",
            "AWF_OPENCODE_OLLAMA_BASE_URL": "http://worker-daemon:11434",
        },
        {
            "name": "ollama-host-lease",
            "secrets": [
                {
                    "name": "ollama-url",
                    "kind": "env",
                    "target": "OLLAMA_HOST",
                    "ref": "env/HOST_OLLAMA_URL",
                    "provider": "env",
                }
            ],
        },
    )

    assert result["OLLAMA_HOST"] == "http://ollama-sidecar:11434"
    # The leased ``OLLAMA_HOST`` owns the daemon selection, so the inherited higher-precedence
    # worker ``AWF_OPENCODE_OLLAMA_BASE_URL`` must be masked.
    assert "AWF_OPENCODE_OLLAMA_BASE_URL" not in result


@pytest.mark.unit
def test_overlay_profile_ollama_base_url_env_lease_runtime_wins() -> None:
    """``runtime.environment`` is first-writer-wins over secret leases in the agent env
    (``merge_agent_environment``), so when both declare the same Ollama base URL key the overlay
    keeps the runtime value rather than the lease's host source."""
    result = provider_readiness_helpers.overlay_profile_ollama_base_url(
        {"HOST_OLLAMA_URL": "http://ollama-sidecar:11434"},
        {
            "name": "ollama-host-both",
            "runtime": {"environment": {"OLLAMA_HOST": "http://runtime-daemon:11434"}},
            "secrets": [
                {
                    "name": "ollama-url",
                    "kind": "env",
                    "target": "OLLAMA_HOST",
                    "ref": "env/HOST_OLLAMA_URL",
                    "provider": "env",
                }
            ],
        },
    )

    assert result["OLLAMA_HOST"] == "http://runtime-daemon:11434"


@pytest.mark.unit
def test_overlay_profile_ollama_base_url_env_lease_unresolvable_undeclared() -> None:
    """An env secret lease whose host source is absent/empty, whose ref is unparseable, or whose
    provider is not ``env`` is treated as undeclared — the launcher omits an optional lease and
    fails a required one, but this best-effort overlay only surfaces (and only masks behind) a
    daemon URL it can actually resolve from the worker environ. A non-Ollama lease target is
    ignored and a non-``env`` lease kind is left alone, so the inherited worker value survives."""
    environ = {"AWF_OPENCODE_OLLAMA_BASE_URL": "http://worker-daemon:11434"}

    result = provider_readiness_helpers.overlay_profile_ollama_base_url(
        environ,
        {
            "name": "ollama-host-unresolvable",
            "secrets": [
                {
                    "name": "missing-source",
                    "kind": "env",
                    "target": "OLLAMA_HOST",
                    "ref": "env/HOST_OLLAMA_URL",
                    "provider": "env",
                },
                {
                    "name": "bad-ref",
                    "kind": "env",
                    "target": "OLLAMA_HOST",
                    "ref": "local-file:/host/ollama",
                    "provider": "env",
                },
                {
                    "name": "non-env-provider",
                    "kind": "env",
                    "target": "OLLAMA_HOST",
                    "ref": "env/HOST_OLLAMA_URL",
                    "provider": "vault",
                },
                {
                    "name": "mounted",
                    "kind": "mount",
                    "target": "OLLAMA_HOST",
                    "ref": "local-file:/host/ollama",
                    "provider": "env",
                },
            ],
        },
    )

    assert result == environ


@pytest.mark.unit
def test_overlay_profile_provider_credentials_overlays_profile_declared_key() -> None:
    """A provider API key declared solely in the profile's runtime.environment reaches
    the agent container but not the worker process the non-Ollama credential gate runs
    in. The overlay brings the profile-declared key into the readiness environ so a
    workspace is not blocked with OPENCODE_PROVIDER_AUTH_MISSING for a credential the
    agent would use — symmetric to overlay_profile_ollama_base_url."""
    result = provider_readiness_helpers.overlay_profile_provider_credentials(
        {},
        {
            "name": "opencode-openai",
            "runtime": {"environment": {"OPENAI_API_KEY": "sk-proj-profile-only"}},
        },
    )

    assert result["OPENAI_API_KEY"] == "sk-proj-profile-only"


@pytest.mark.unit
def test_overlay_profile_provider_credentials_overlays_ollama_cloud_key() -> None:
    """An ``OLLAMA_API_KEY`` declared solely in the profile env reaches the agent
    container but not the worker. A ``:cloud`` Ollama model whose sidecar base URL is
    unreachable from the worker needs that credential visible for the host-probe defer
    path; without the overlay the credential gate would falsely block the workspace with
    OPENCODE_OLLAMA_AUTH_MISSING. The overlay brings the profile-declared Ollama Cloud
    key into the readiness environ alongside the non-Ollama provider keys."""
    result = provider_readiness_helpers.overlay_profile_provider_credentials(
        {},
        {
            "name": "opencode-ollama-cloud",
            "runtime": {
                "environment": {
                    "AWF_OPENCODE_OLLAMA_BASE_URL": "http://ollama-sidecar:11434",
                    "OLLAMA_API_KEY": "ollama-cloud-profile-only",
                },
            },
        },
    )

    assert result["OLLAMA_API_KEY"] == "ollama-cloud-profile-only"


@pytest.mark.unit
def test_overlay_profile_provider_credentials_resolves_placeholder_against_environ() -> None:
    """Profile env values may carry Compose-style ``${NAME}`` placeholders resolved by
    the agent container. The worker-side overlay resolves them against ``environ``; an
    unresolvable required placeholder is treated as undeclared, not surfaced as a 500."""
    declared = provider_readiness_helpers.overlay_profile_provider_credentials(
        {"HOST_ANTHROPIC_KEY": "sk-ant-host"},
        {
            "name": "opencode-anthropic",
            "runtime": {"environment": {"ANTHROPIC_API_KEY": "${HOST_ANTHROPIC_KEY}"}},
        },
    )
    assert declared["ANTHROPIC_API_KEY"] == "sk-ant-host"

    undeclared = provider_readiness_helpers.overlay_profile_provider_credentials(
        {},
        {
            "name": "opencode-anthropic",
            "runtime": {"environment": {"ANTHROPIC_API_KEY": "${MISSING_KEY:?set MISSING_KEY}"}},
        },
    )
    assert "ANTHROPIC_API_KEY" not in undeclared

    # A plain ``${NAME}`` placeholder resolving to empty is also treated as undeclared.
    empty = provider_readiness_helpers.overlay_profile_provider_credentials(
        {},
        {
            "name": "opencode-anthropic",
            "runtime": {"environment": {"ANTHROPIC_API_KEY": "${MISSING_KEY}"}},
        },
    )
    assert "ANTHROPIC_API_KEY" not in empty


@pytest.mark.unit
def test_overlay_profile_provider_credentials_profile_owned_key_masks_inherited() -> None:
    """A profile-owned key that resolves empty masks an inherited worker/service value.

    When the worker environ already carries a provider key (e.g. OPENAI_API_KEY) and the
    selected profile declares the same key as a literal empty string, an unset required
    placeholder, or one resolving empty, ``runtime.environment`` owns the agent's slot
    (first-writer-wins). The launcher renders only the profile value into the agent, so the
    inherited worker credential never reaches it. The overlay must drop the inherited value
    rather than leave it in the readiness environ, otherwise create/retry preflight would
    admit a workspace on a credential the agent will not receive."""
    literal_empty = provider_readiness_helpers.overlay_profile_provider_credentials(
        {"OPENAI_API_KEY": "sk-proj-worker-inherited"},
        {
            "name": "opencode-openai-mask-literal-empty",
            "runtime": {"environment": {"OPENAI_API_KEY": ""}},
        },
    )
    assert "OPENAI_API_KEY" not in literal_empty

    required_placeholder = provider_readiness_helpers.overlay_profile_provider_credentials(
        {"OPENAI_API_KEY": "sk-proj-worker-inherited"},
        {
            "name": "opencode-openai-mask-required-placeholder",
            "runtime": {"environment": {"OPENAI_API_KEY": "${MISSING_KEY:?set MISSING_KEY}"}},
        },
    )
    assert "OPENAI_API_KEY" not in required_placeholder

    empty_placeholder = provider_readiness_helpers.overlay_profile_provider_credentials(
        {"OPENAI_API_KEY": "sk-proj-worker-inherited"},
        {
            "name": "opencode-openai-mask-empty-placeholder",
            "runtime": {"environment": {"OPENAI_API_KEY": "${MISSING_KEY}"}},
        },
    )
    assert "OPENAI_API_KEY" not in empty_placeholder


@pytest.mark.unit
def test_overlay_profile_provider_credentials_no_profile_returns_environ_unchanged() -> None:
    """A workspace without a resolved profile (or an unvalidatable snapshot) falls back
    to the supplied environ unchanged."""
    environ = {"OPENAI_API_KEY": "sk-proj-worker"}

    assert provider_readiness_helpers.overlay_profile_provider_credentials(environ, None) == (
        environ
    )
    assert (
        provider_readiness_helpers.overlay_profile_provider_credentials(
            environ, {"not": "a valid profile snapshot"}
        )
        == environ
    )


@pytest.mark.unit
def test_overlay_profile_provider_credentials_overlays_env_secret_lease() -> None:
    """A provider key supplied through a profile ``kind="env"`` secret lease (target
    ``OPENAI_API_KEY``, ``ref="env/HOST_OPENAI_KEY"``) reaches the agent via the launcher's
    secret-lease environment merge, not ``runtime.environment``. The overlay must resolve
    the lease's host source against ``environ`` too, so admission does not block such a
    workspace with OPENCODE_PROVIDER_AUTH_MISSING before the lease is resolved."""
    result = provider_readiness_helpers.overlay_profile_provider_credentials(
        {"HOST_OPENAI_KEY": "sk-proj-host-lease"},
        {
            "name": "opencode-openai-lease",
            "secrets": [
                {
                    "name": "openai-key",
                    "kind": "env",
                    "target": "OPENAI_API_KEY",
                    "ref": "env/HOST_OPENAI_KEY",
                    "provider": "env",
                }
            ],
        },
    )

    assert result["OPENAI_API_KEY"] == "sk-proj-host-lease"


@pytest.mark.unit
def test_overlay_profile_provider_credentials_overlays_ollama_cloud_env_lease() -> None:
    """``OLLAMA_API_KEY`` supplied through a ``kind="env"`` secret lease is overlaid for the
    same reason as the non-Ollama provider keys: a ``:cloud`` Ollama model whose sidecar is
    unreachable from the worker needs the credential visible for the host-probe defer path."""
    result = provider_readiness_helpers.overlay_profile_provider_credentials(
        {"HOST_OLLAMA_KEY": "ollama-cloud-host-lease"},
        {
            "name": "opencode-ollama-cloud-lease",
            "secrets": [
                {
                    "name": "ollama-key",
                    "kind": "env",
                    "target": "OLLAMA_API_KEY",
                    "ref": "env/HOST_OLLAMA_KEY",
                    "provider": "env",
                }
            ],
        },
    )

    assert result["OLLAMA_API_KEY"] == "ollama-cloud-host-lease"


@pytest.mark.unit
def test_overlay_profile_provider_credentials_env_lease_runtime_wins() -> None:
    """``runtime.environment`` is first-writer-wins over secret leases in the agent env
    (``merge_agent_environment``), so when both declare the same target the overlay keeps
    the runtime value rather than the lease's host source."""
    result = provider_readiness_helpers.overlay_profile_provider_credentials(
        {"HOST_OPENAI_KEY": "sk-proj-host-lease"},
        {
            "name": "opencode-openai-both",
            "runtime": {"environment": {"OPENAI_API_KEY": "sk-proj-runtime"}},
            "secrets": [
                {
                    "name": "openai-key",
                    "kind": "env",
                    "target": "OPENAI_API_KEY",
                    "ref": "env/HOST_OPENAI_KEY",
                    "provider": "env",
                }
            ],
        },
    )

    assert result["OPENAI_API_KEY"] == "sk-proj-runtime"


@pytest.mark.unit
def test_overlay_profile_provider_credentials_env_lease_unresolvable_runtime_wins() -> None:
    """``runtime.environment`` owns the key even when its value cannot be resolved here.

    When ``runtime.environment`` declares the provider key with an unset required
    placeholder (``${MISSING:?set}``) — or one resolving empty — the launcher's
    first-writer-wins merge keeps the failing runtime placeholder and drops the lease, so
    the agent never receives the lease's host credential. The overlay must not surface the
    lease's value, otherwise create/retry preflight would admit a workspace with credentials
    the agent will not actually receive."""
    required = provider_readiness_helpers.overlay_profile_provider_credentials(
        {"HOST_OPENAI_KEY": "sk-proj-host-lease"},
        {
            "name": "opencode-openai-required-placeholder",
            "runtime": {"environment": {"OPENAI_API_KEY": "${MISSING_KEY:?set MISSING_KEY}"}},
            "secrets": [
                {
                    "name": "openai-key",
                    "kind": "env",
                    "target": "OPENAI_API_KEY",
                    "ref": "env/HOST_OPENAI_KEY",
                    "provider": "env",
                }
            ],
        },
    )

    assert "OPENAI_API_KEY" not in required

    empty = provider_readiness_helpers.overlay_profile_provider_credentials(
        {"HOST_OPENAI_KEY": "sk-proj-host-lease"},
        {
            "name": "opencode-openai-empty-placeholder",
            "runtime": {"environment": {"OPENAI_API_KEY": "${MISSING_KEY}"}},
            "secrets": [
                {
                    "name": "openai-key",
                    "kind": "env",
                    "target": "OPENAI_API_KEY",
                    "ref": "env/HOST_OPENAI_KEY",
                    "provider": "env",
                }
            ],
        },
    )

    assert "OPENAI_API_KEY" not in empty

    # A *literal* empty string (``OPENAI_API_KEY: ""``) is still a declaration: the
    # launcher's first-writer-wins merge keeps the empty runtime slot and drops the lease,
    # so the overlay must record the key on presence (not truthiness) and refrain from
    # surfacing the lease's host credential — otherwise preflight would admit a workspace
    # whose agent receives only the empty runtime value.
    literal_empty = provider_readiness_helpers.overlay_profile_provider_credentials(
        {"HOST_OPENAI_KEY": "sk-proj-host-lease"},
        {
            "name": "opencode-openai-literal-empty",
            "runtime": {"environment": {"OPENAI_API_KEY": ""}},
            "secrets": [
                {
                    "name": "openai-key",
                    "kind": "env",
                    "target": "OPENAI_API_KEY",
                    "ref": "env/HOST_OPENAI_KEY",
                    "provider": "env",
                }
            ],
        },
    )

    assert "OPENAI_API_KEY" not in literal_empty


@pytest.mark.unit
def test_overlay_profile_provider_credentials_env_lease_missing_source_undeclared() -> None:
    """An env secret lease whose host source is absent (or empty) from ``environ`` is treated
    as undeclared — the launcher omits an optional lease and fails a required one, but the
    best-effort admission overlay simply does not surface a credential it cannot resolve.
    A non-provider lease target is ignored, and a non-``env`` lease kind is left alone."""
    result = provider_readiness_helpers.overlay_profile_provider_credentials(
        {},
        {
            "name": "opencode-openai-missing",
            "secrets": [
                {
                    "name": "openai-key",
                    "kind": "env",
                    "target": "OPENAI_API_KEY",
                    "ref": "env/HOST_OPENAI_KEY",
                    "provider": "env",
                },
                {
                    "name": "unrelated",
                    "kind": "env",
                    "target": "SOME_OTHER_VAR",
                    "ref": "env/HOST_OTHER",
                    "provider": "env",
                },
                {
                    "name": "mounted-key",
                    "kind": "mount",
                    "target": "/run/secrets/openai",
                    "ref": "local-file:/host/openai",
                    "provider": "local-file",
                },
            ],
        },
    )

    assert "OPENAI_API_KEY" not in result


@pytest.mark.unit
def test_overlay_profile_provider_credentials_env_lease_requires_provider_env() -> None:
    """A ``kind="env"`` lease targeting a provider key only reaches the agent when it routes
    through the launcher's env path (``provider: env`` ⇒ ``node.secret_mounts._ENV_PROVIDERS``).
    A lease that omits ``provider`` is skipped by the launcher (``_normalized_provider`` returns
    ``None``), and a non-``env`` provider routes to a different handler or is rejected — so
    neither injects this env key into the agent. The overlay must gate on the same ``provider:
    env`` semantics; otherwise create/retry preflight would admit an openai/... workspace on a
    host credential the agent container never receives."""
    omitted_provider = provider_readiness_helpers.overlay_profile_provider_credentials(
        {"HOST_OPENAI_KEY": "sk-proj-host-lease"},
        {
            "name": "opencode-openai-no-provider",
            "secrets": [
                {
                    "name": "openai-key",
                    "kind": "env",
                    "target": "OPENAI_API_KEY",
                    "ref": "env/HOST_OPENAI_KEY",
                }
            ],
        },
    )

    assert "OPENAI_API_KEY" not in omitted_provider

    non_env_provider = provider_readiness_helpers.overlay_profile_provider_credentials(
        {"HOST_OPENAI_KEY": "sk-proj-host-lease"},
        {
            "name": "opencode-openai-github-provider",
            "secrets": [
                {
                    "name": "openai-key",
                    "kind": "env",
                    "target": "OPENAI_API_KEY",
                    "ref": "env/HOST_OPENAI_KEY",
                    "provider": "github",
                }
            ],
        },
    )

    assert "OPENAI_API_KEY" not in non_env_provider
