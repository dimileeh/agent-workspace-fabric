"""Unit coverage for pure helpers on profile model value objects."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from awf.profiles.models import (
    ProfileAppEndpoint,
    ProfileAppEndpointHealth,
    ProfileHealthCheck,
    ProfileRuntime,
    normalize_inline_profile_snapshot,
)


@pytest.mark.unit
def test_command_healthcheck_display_and_target_return_command() -> None:
    check = ProfileHealthCheck(name="db", command="pg_isready -U awf")

    assert check.display_command() == "pg_isready -U awf"
    assert check.target() == "pg_isready -U awf"


@pytest.mark.unit
def test_url_healthcheck_target_passes_through_when_no_userinfo() -> None:
    check = ProfileHealthCheck(name="api", url="http://api:8080/health")

    assert check.target() == "http://api:8080/health"
    assert check.display_command() == "GET http://api:8080/health expected 200"


@pytest.mark.unit
def test_url_healthcheck_target_redacts_userinfo() -> None:
    check = ProfileHealthCheck(name="api", url="http://user:secret@api:8080/health")

    assert check.target() == "http://api:8080/health"
    assert check.display_command() == "GET http://api:8080/health expected 200"


@pytest.mark.unit
def test_app_endpoint_health_normalizes_method_to_uppercase() -> None:
    health = ProfileAppEndpointHealth(path="/health", method="get")

    assert health.method == "GET"


@pytest.mark.unit
def test_app_endpoint_normalizes_scheme_to_lowercase() -> None:
    endpoint = ProfileAppEndpoint(name="web", service="app", port=8080, scheme="HTTPS")

    assert endpoint.scheme == "https"


@pytest.mark.unit
def test_runtime_toolchains_absent_defaults_to_empty_no_behavior_change() -> None:
    """An absent toolchains declaration is a no-op: the runtime keeps its prior shape."""
    runtime = ProfileRuntime()

    assert runtime.toolchains == {}
    # Round-trips through dump/validate without gaining the field as anything but {}.
    assert ProfileRuntime.model_validate(runtime.model_dump()).toolchains == {}


@pytest.mark.unit
def test_runtime_toolchains_explicit_none_is_treated_as_empty() -> None:
    """An explicit ``toolchains: null`` (e.g. an empty YAML key) means no requirement."""
    runtime = ProfileRuntime.model_validate({"toolchains": None})

    assert runtime.toolchains == {}


@pytest.mark.unit
def test_runtime_browsers_absent_none_and_empty_are_noops() -> None:
    assert ProfileRuntime().browsers == []
    assert ProfileRuntime.model_validate({"browsers": None}).browsers == []
    assert ProfileRuntime.model_validate({"browsers": []}).browsers == []


@pytest.mark.unit
def test_runtime_browsers_accepts_allowlist_lowercases_and_dedupes() -> None:
    runtime = ProfileRuntime.model_validate(
        {"browsers": ["CHROMIUM", "firefox", "chromium", "WebKit"]}
    )

    assert runtime.browsers == ["chromium", "firefox", "webkit"]


@pytest.mark.unit
@pytest.mark.parametrize("bad_browser", ["chrome", "edge", "", " chromium "])
def test_runtime_browsers_rejects_unknown_names(bad_browser: str) -> None:
    with pytest.raises(ValidationError):
        ProfileRuntime.model_validate({"browsers": [bad_browser]})


@pytest.mark.unit
@pytest.mark.parametrize("bad_value", ["chromium", {"name": "chromium"}, [17]])
def test_runtime_browsers_rejects_invalid_shapes(bad_value: object) -> None:
    with pytest.raises(ValidationError):
        ProfileRuntime.model_validate({"browsers": bad_value})


@pytest.mark.unit
def test_runtime_browsers_caps_declaration_count_before_deduping() -> None:
    with pytest.raises(ValidationError):
        ProfileRuntime.model_validate({"browsers": ["chromium"] * 9})


@pytest.mark.unit
def test_runtime_toolchains_rejects_non_string_language_key() -> None:
    with pytest.raises(ValidationError):
        ProfileRuntime.model_validate({"toolchains": {17: ["17"]}})


@pytest.mark.unit
def test_runtime_toolchains_normalizes_lowercases_and_dedupes_preserving_order() -> None:
    runtime = ProfileRuntime.model_validate({"toolchains": {"Java": ["17", "21", "17"]}})

    assert runtime.toolchains == {"java": ["17", "21"]}


@pytest.mark.unit
def test_runtime_toolchains_accepts_dotted_numeric_versions() -> None:
    runtime = ProfileRuntime.model_validate({"toolchains": {"java": ["1.8", "11.0.2"]}})

    assert runtime.toolchains == {"java": ["1.8", "11.0.2"]}


@pytest.mark.unit
@pytest.mark.parametrize("bad_version", ["17a", "", "  ", "v17", "17.", ".17"])
def test_runtime_toolchains_rejects_malformed_version(bad_version: str) -> None:
    with pytest.raises(ValidationError):
        ProfileRuntime.model_validate({"toolchains": {"java": [bad_version]}})


@pytest.mark.unit
def test_runtime_toolchains_rejects_empty_version_list() -> None:
    with pytest.raises(ValidationError):
        ProfileRuntime.model_validate({"toolchains": {"java": []}})


@pytest.mark.unit
@pytest.mark.parametrize("bad_language", ["", "  ", "17java", "java lang", "Java!"])
def test_runtime_toolchains_rejects_blank_or_invalid_language(bad_language: str) -> None:
    with pytest.raises(ValidationError):
        ProfileRuntime.model_validate({"toolchains": {bad_language: ["17"]}})


@pytest.mark.unit
def test_runtime_toolchains_rejects_non_mapping() -> None:
    with pytest.raises(ValidationError):
        ProfileRuntime.model_validate({"toolchains": ["java"]})


@pytest.mark.unit
def test_runtime_toolchains_accepts_non_dict_mapping() -> None:
    """The guard accepts any Mapping (e.g. ``MappingProxyType``), not only ``dict``."""
    from types import MappingProxyType

    runtime = ProfileRuntime.model_validate(
        {"toolchains": MappingProxyType({"Java": ["17", "21"]})}
    )

    assert runtime.toolchains == {"java": ["17", "21"]}


@pytest.mark.unit
def test_runtime_toolchains_rejects_non_list_versions() -> None:
    with pytest.raises(ValidationError):
        ProfileRuntime.model_validate({"toolchains": {"java": "17"}})


@pytest.mark.unit
def test_runtime_toolchains_rejects_non_string_version_entry() -> None:
    with pytest.raises(ValidationError):
        ProfileRuntime.model_validate({"toolchains": {"java": [17]}})


@pytest.mark.unit
def test_runtime_toolchains_rejects_too_many_languages() -> None:
    too_many = {f"lang{i}": ["1"] for i in range(17)}
    with pytest.raises(ValidationError):
        ProfileRuntime.model_validate({"toolchains": too_many})


@pytest.mark.unit
def test_runtime_toolchains_rejects_too_many_versions() -> None:
    with pytest.raises(ValidationError):
        ProfileRuntime.model_validate({"toolchains": {"java": [str(i) for i in range(17)]}})


@pytest.mark.unit
def test_runtime_toolchains_rejects_duplicate_language_after_lowercasing() -> None:
    with pytest.raises(ValidationError):
        ProfileRuntime.model_validate({"toolchains": {"Java": ["17"], "java": ["21"]}})


@pytest.mark.unit
def test_runtime_extra_keys_still_forbidden() -> None:
    """Regression: adding ``toolchains`` must not loosen ``extra='forbid'``."""
    with pytest.raises(ValidationError):
        ProfileRuntime.model_validate({"unknown_runtime_key": "value"})


@pytest.mark.unit
def test_normalize_inline_profile_snapshot_passes_through_none() -> None:
    assert normalize_inline_profile_snapshot(None) is None


@pytest.mark.unit
def test_normalize_inline_profile_snapshot_defaults_missing_forge_to_auto() -> None:
    """A pre-forge legacy snapshot lacks the key; normalization adds the input
    default so it compares equal to a fresh replay that dumps ``forge="auto"``."""
    legacy = {"name": "inline"}

    normalized = normalize_inline_profile_snapshot(legacy)

    assert normalized == {"name": "inline", "forge": "auto"}
    # The input snapshot (a live ORM attribute at the call sites) must not mutate.
    assert legacy == {"name": "inline"}


@pytest.mark.unit
def test_normalize_inline_profile_snapshot_preserves_present_forge() -> None:
    explicit = {"name": "inline", "forge": "github"}

    normalized = normalize_inline_profile_snapshot(explicit)

    assert normalized == {"name": "inline", "forge": "github"}
    assert normalized is not explicit


@pytest.mark.unit
def test_normalize_inline_profile_snapshot_backfills_missing_monitor_grace() -> None:
    """A pre-#655 legacy snapshot's ``monitor`` lacks the grace key; normalization
    adds the input default so it compares equal to a fresh replay that dumps
    ``awaiting_required_checks_grace_seconds=600.0``."""
    legacy = {"name": "inline", "forge": "auto", "monitor": {"require_ci": True}}

    normalized = normalize_inline_profile_snapshot(legacy)

    assert normalized == {
        "name": "inline",
        "forge": "auto",
        "monitor": {
            "require_ci": True,
            "awaiting_required_checks_grace_seconds": 600.0,
        },
    }
    # The input snapshot (a live ORM attribute at the call sites) must not mutate.
    assert legacy == {"name": "inline", "forge": "auto", "monitor": {"require_ci": True}}


@pytest.mark.unit
def test_normalize_inline_profile_snapshot_preserves_present_monitor_grace() -> None:
    """A snapshot whose ``monitor`` already carries the grace value is unchanged
    by normalization (other than a shallow copy)."""
    explicit = {
        "name": "inline",
        "forge": "auto",
        "monitor": {"awaiting_required_checks_grace_seconds": 120.0, "require_ci": True},
    }

    normalized = normalize_inline_profile_snapshot(explicit)

    assert normalized == explicit
    assert normalized is not explicit
    assert normalized["monitor"] == explicit["monitor"]


@pytest.mark.unit
def test_normalize_inline_profile_snapshot_backfills_both_forge_and_monitor_grace() -> None:
    """A truly legacy snapshot (pre-forge AND pre-#655) gets both backfills so it
    compares equal to a fresh replay."""
    legacy = {"name": "inline", "monitor": {"require_ci": True}}

    normalized = normalize_inline_profile_snapshot(legacy)

    assert normalized == {
        "name": "inline",
        "forge": "auto",
        "monitor": {
            "require_ci": True,
            "awaiting_required_checks_grace_seconds": 600.0,
        },
    }
    assert legacy == {"name": "inline", "monitor": {"require_ci": True}}


@pytest.mark.unit
def test_normalize_inline_profile_snapshot_skips_monitor_when_not_a_dict() -> None:
    """A malformed ``monitor`` that is not a dict must not crash normalization; it
    is passed through untouched so downstream validation surfaces the problem."""
    malformed = {"name": "inline", "forge": "auto", "monitor": "not-a-dict"}

    normalized = normalize_inline_profile_snapshot(malformed)

    assert normalized == malformed
