"""Compose volume name translation/sanitization for hosted validation payloads."""

from __future__ import annotations

import hashlib
import re
from collections import Counter
from collections.abc import Iterator, Mapping
from typing import Any

from awf.common.redaction import redact_secrets

_HOSTED_DNS1123_LABEL_PATTERN = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
_HOSTED_COMPOSE_INTERPOLATED_TARGET_PATTERN = re.compile(
    r"^\$(?:\{[A-Za-z_][A-Za-z0-9_]*|[A-Za-z_][A-Za-z0-9_]*)"
)
_HOSTED_VOLUME_INVALID_RUN_PATTERN = re.compile(r"[^a-z0-9-]+")
_HOSTED_VOLUME_HYPHEN_RUN_PATTERN = re.compile(r"-+")
_HOSTED_VOLUME_HASH_LENGTHS = (10, 12, 16, 20, 24, 32)
_HOSTED_KUBERNETES_LABEL_MAX_LENGTH = 63
_HOSTED_REDACTED_VOLUME_NAME = "redacted"
_HOSTED_REDACTED_VOLUME_NAME_PATTERN = re.compile(
    rf"^{_HOSTED_REDACTED_VOLUME_NAME}(?:-(?:[2-9]|[1-9][0-9]+))?$"
)


def _hosted_validation_compose_volume_name_translations(
    compose: Mapping[object, object],
) -> dict[str, str]:
    volume_names = set(_hosted_validation_compose_volume_names(compose))
    if not volume_names:
        return {}

    normalized_names = {
        name: _hosted_validation_normalized_compose_volume_name(name) for name in volume_names
    }
    candidates = {
        name: _hosted_validation_bounded_compose_volume_name(normalized_name)
        for name, normalized_name in normalized_names.items()
    }
    candidate_counts = Counter(candidates.values())
    translations: dict[str, str] = {}
    used_names: dict[str, str] = {}

    for name in sorted(volume_names):
        if _hosted_validation_dns1123_label_is_valid(name):
            translations[name] = name
            used_names[name] = name

    for name in sorted(volume_names):
        if name in translations:
            continue
        candidate = candidates[name]
        if candidate_counts[candidate] == 1 and candidate not in used_names:
            translated_name = candidate
        else:
            translated_name = _hosted_validation_disambiguated_compose_volume_name(
                normalized_base=normalized_names[name],
                original_name=name,
                used_names=used_names,
            )
        previous_original = used_names.get(translated_name)
        if previous_original is not None and previous_original != name:  # pragma: no cover
            raise ValueError("hosted rendered stack volume name collision")
        translations[name] = translated_name
        used_names[translated_name] = name

    return _hosted_validation_redacted_volume_translations(translations)


def _hosted_validation_redacted_volume_translations(
    translations: Mapping[str, str],
) -> dict[str, str]:
    redacted_names = {
        name
        for name, translated_name in translations.items()
        if _hosted_validation_compose_volume_name_needs_redaction(
            name,
            translated_name=translated_name,
        )
    }
    used_names = {
        translated_name
        for name, translated_name in translations.items()
        if name not in redacted_names
    }
    if (
        not redacted_names
        or len(redacted_names) == 1
        and _HOSTED_REDACTED_VOLUME_NAME not in used_names
    ):
        return dict(translations)

    payload = dict(translations)
    for name in sorted(redacted_names):
        placeholder = _hosted_validation_next_redacted_volume_name(used_names)
        payload[name] = placeholder
        used_names.add(placeholder)
    return payload


def _hosted_validation_next_redacted_volume_name(used_names: set[str]) -> str:
    index = 1
    while True:
        candidate = (
            _HOSTED_REDACTED_VOLUME_NAME
            if index == 1
            else f"{_HOSTED_REDACTED_VOLUME_NAME}-{index}"
        )
        if candidate not in used_names:
            return candidate
        index += 1


def _hosted_validation_compose_volume_names(
    compose: Mapping[object, object],
) -> Iterator[str]:
    volumes = compose.get("volumes")
    if isinstance(volumes, Mapping):
        for name, volume in volumes.items():
            yield str(name)
            explicit_name = _hosted_validation_compose_volume_declaration_explicit_name(volume)
            if explicit_name is not None:
                yield explicit_name

    services = compose.get("services")
    if not isinstance(services, Mapping):
        return
    for name, service in services.items():
        if str(name) == "agent" or not isinstance(service, Mapping):
            continue
        yield from _hosted_validation_compose_service_volume_names(service)


def _hosted_validation_compose_service_volume_names(
    service: Mapping[object, object],
) -> Iterator[str]:
    volumes = service.get("volumes")
    if not isinstance(volumes, list):
        return
    for volume in volumes:
        if isinstance(volume, str):
            source = _hosted_validation_compose_short_named_volume_source(volume)
        elif isinstance(volume, Mapping):
            source = _hosted_validation_compose_mapping_named_volume_source(volume)
        else:
            source = None
        if source is not None:
            yield source


def _hosted_validation_compose_volume_declaration_explicit_name(
    volume: object,
) -> str | None:
    if not isinstance(volume, Mapping):
        return None
    name = volume.get("name")
    if not isinstance(name, str) or not name:
        return None
    return name


def _hosted_validation_dns1123_label_is_valid(value: str) -> bool:
    return _HOSTED_DNS1123_LABEL_PATTERN.fullmatch(value) is not None


def _hosted_validation_normalized_compose_volume_name(value: str) -> str:
    normalized = _HOSTED_VOLUME_INVALID_RUN_PATTERN.sub("-", value.lower())
    normalized = _HOSTED_VOLUME_HYPHEN_RUN_PATTERN.sub("-", normalized).strip("-")
    return normalized or "volume"


def _hosted_validation_bounded_compose_volume_name(value: str) -> str:
    return value[:_HOSTED_KUBERNETES_LABEL_MAX_LENGTH].rstrip("-") or "volume"


def _hosted_validation_disambiguated_compose_volume_name(
    *,
    normalized_base: str,
    original_name: str,
    used_names: Mapping[str, str],
) -> str:
    digest = hashlib.sha256(original_name.encode("utf-8")).hexdigest()
    for hash_length in _HOSTED_VOLUME_HASH_LENGTHS:
        suffix = f"-{digest[:hash_length]}"
        max_prefix_length = _HOSTED_KUBERNETES_LABEL_MAX_LENGTH - len(suffix)
        if max_prefix_length <= 0:  # pragma: no cover
            continue
        prefix = normalized_base[:max_prefix_length].rstrip("-") or "volume"
        candidate = f"{prefix}{suffix}"
        if _hosted_validation_dns1123_label_is_valid(candidate) and candidate not in used_names:
            return candidate
    raise ValueError("hosted rendered stack volume name collision")


def _hosted_validation_sanitize_rendered_stack_volumes(
    volumes: Mapping[object, object],
    *,
    volume_translations: Mapping[str, str],
) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    for name, value in volumes.items():
        volume_name = str(name)
        sanitized_name = _hosted_validation_sanitize_compose_volume_name(
            volume_name,
            volume_translations=volume_translations,
        )
        if sanitized_name in payload:
            raise ValueError("hosted rendered stack volume declaration collision")
        payload[sanitized_name] = _hosted_validation_sanitize_rendered_stack_volume(
            value,
            volume_translations=volume_translations,
        )
    return payload


def _hosted_validation_sanitize_rendered_stack_volume(
    volume: object,
    *,
    volume_translations: Mapping[str, str],
) -> Any:
    if not isinstance(volume, Mapping):
        return _hosted_validation_sanitize_compose_value(volume)
    payload: dict[str, Any] = {}
    for key, value in volume.items():
        field = str(key)
        if field == "name" and isinstance(value, str):
            payload[field] = _hosted_validation_sanitize_compose_volume_name(
                value,
                volume_translations=volume_translations,
            )
            continue
        payload[field] = _hosted_validation_sanitize_compose_value(value)
    return payload


def _hosted_validation_sanitize_compose_volume_name(
    name: str,
    *,
    volume_translations: Mapping[str, str],
) -> str:
    translated_name = volume_translations.get(name, name)
    if _hosted_validation_compose_volume_name_needs_redaction(
        name,
        translated_name=translated_name,
    ):
        if _HOSTED_REDACTED_VOLUME_NAME_PATTERN.fullmatch(translated_name):
            return translated_name
        return _HOSTED_REDACTED_VOLUME_NAME
    return translated_name


def _hosted_validation_compose_volume_name_needs_redaction(
    name: str,
    *,
    translated_name: str,
) -> bool:
    return redact_secrets(name) != name or redact_secrets(translated_name) != translated_name


def _hosted_validation_sanitize_compose_service_volumes(
    volumes: object,
    *,
    volume_translations: Mapping[str, str],
) -> Any:
    if not isinstance(volumes, list):
        return _hosted_validation_sanitize_compose_value(volumes)
    return [
        _hosted_validation_sanitize_compose_service_volume(
            volume,
            volume_translations=volume_translations,
        )
        for volume in volumes
    ]


def _hosted_validation_sanitize_compose_service_volume(
    volume: object,
    *,
    volume_translations: Mapping[str, str],
) -> Any:
    if isinstance(volume, str):
        return _hosted_validation_sanitize_compose_short_volume(
            volume,
            volume_translations=volume_translations,
        )
    if isinstance(volume, Mapping):
        return _hosted_validation_sanitize_compose_volume_mapping(
            volume,
            volume_translations=volume_translations,
        )
    return _hosted_validation_sanitize_compose_value(volume)


def _hosted_validation_sanitize_compose_short_volume(
    volume: str,
    *,
    volume_translations: Mapping[str, str],
) -> str:
    source = _hosted_validation_compose_short_named_volume_source(volume)
    if source is None:
        return redact_secrets(volume)
    sanitized_source = _hosted_validation_sanitize_compose_volume_name(
        source,
        volume_translations=volume_translations,
    )
    _source, _separator, remainder = volume.partition(":")
    return redact_secrets(f"{sanitized_source}:{remainder}")


def _hosted_validation_sanitize_compose_volume_mapping(
    volume: Mapping[object, object],
    *,
    volume_translations: Mapping[str, str],
) -> dict[str, Any]:
    source = _hosted_validation_compose_mapping_named_volume_source(volume)
    payload: dict[str, Any] = {}
    for key, value in volume.items():
        field = str(key)
        if field in {"source", "src"} and source is not None and value == source:
            payload[field] = _hosted_validation_sanitize_compose_volume_name(
                source,
                volume_translations=volume_translations,
            )
            continue
        payload[field] = _hosted_validation_sanitize_compose_value(value)
    return payload


def _hosted_validation_compose_short_named_volume_source(volume: str) -> str | None:
    if _hosted_validation_compose_short_volume_is_windows_path(volume):
        return None
    source, separator, remainder = volume.partition(":")
    if (
        not separator
        or not source
        or not _hosted_validation_compose_short_volume_has_container_target(remainder)
    ):
        return None
    if _hosted_validation_compose_volume_source_is_host_path(source):
        return None
    return source


def _hosted_validation_compose_short_volume_is_windows_path(volume: str) -> bool:
    return bool(re.match(r"^[A-Za-z]:[\\/]", volume))


def _hosted_validation_compose_short_volume_has_container_target(target: str) -> bool:
    return target.startswith("/") or bool(_HOSTED_COMPOSE_INTERPOLATED_TARGET_PATTERN.match(target))


def _hosted_validation_compose_mapping_named_volume_source(
    volume: Mapping[object, object],
) -> str | None:
    type_value = volume.get("type")
    if type_value is not None and (
        not isinstance(type_value, str) or type_value.lower() != "volume"
    ):
        return None
    source = volume.get("source")
    if not isinstance(source, str) or not source:
        source = volume.get("src")
    if not isinstance(source, str) or not source:
        return None
    if _hosted_validation_compose_volume_source_is_host_path(source):
        return None
    return source


def _hosted_validation_compose_volume_source_is_host_path(source: str) -> bool:
    return (
        source.startswith(("/", ".", "~", "$")) or "/" in source or "\\" in source or ":" in source
    )


def _hosted_validation_sanitize_compose_value(value: object) -> Any:
    if isinstance(value, Mapping):
        return {
            str(key): _hosted_validation_sanitize_compose_value(child)
            for key, child in value.items()
        }
    if isinstance(value, list):
        return [_hosted_validation_sanitize_compose_value(item) for item in value]
    if isinstance(value, str):
        return redact_secrets(value)
    return value
