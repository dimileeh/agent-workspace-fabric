"""Compose environment helpers for PR monitor handoff resume."""

from __future__ import annotations

import os
import tempfile
from collections.abc import Callable, Mapping
from contextlib import suppress
from pathlib import Path
from typing import Any, cast

import yaml
from yaml.constructor import ConstructorError
from yaml.nodes import MappingNode, ScalarNode


class _ComposeInterpolationPreservingDumper(yaml.SafeDumper):
    """Safe YAML dumper that keeps Compose interpolation scalars active."""


class _ComposeStringKeySafeLoader(yaml.SafeLoader):
    """Safe YAML loader that keeps Compose scalar mapping keys as strings."""


def _construct_compose_string_key_mapping(
    loader: _ComposeStringKeySafeLoader,
    node: MappingNode,
    deep: bool = False,
) -> dict[Any, Any]:
    if not isinstance(node, MappingNode):
        raise ConstructorError(
            None,
            None,
            f"expected a mapping node, but found {node.id}",
            node.start_mark,
        )
    loader.flatten_mapping(node)
    construct_object = cast(Callable[..., Any], loader.construct_object)
    mapping: dict[Any, Any] = {}
    for key_node, value_node in node.value:
        key: Any
        if isinstance(key_node, ScalarNode):
            key = key_node.value
        else:
            key = construct_object(key_node, deep=deep)
        try:
            hash(key)
        except TypeError as exc:
            raise ConstructorError(
                "while constructing a mapping",
                node.start_mark,
                "found unhashable key",
                key_node.start_mark,
            ) from exc
        mapping[key] = construct_object(
            value_node,
            deep=deep,
        )
    return mapping


_ComposeStringKeySafeLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_compose_string_key_mapping,
)


def _represent_compose_interpolation_string(
    dumper: _ComposeInterpolationPreservingDumper,
    value: str,
) -> Any:
    style = '"' if "${" in value else None
    return dumper.represent_scalar("tag:yaml.org,2002:str", value, style=style)


_ComposeInterpolationPreservingDumper.add_representer(
    str,
    _represent_compose_interpolation_string,
)


def _safe_dump_compose_payload_for_resume(payload: object) -> str:
    return yaml.dump(
        payload,
        Dumper=_ComposeInterpolationPreservingDumper,
        sort_keys=False,
    )


def _safe_load_compose_payload_for_resume(text: str) -> object:
    return yaml.load(text, Loader=_ComposeStringKeySafeLoader)


def _atomic_write_text(path: Path, text: str, *, encoding: str) -> None:
    tmp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding=encoding,
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as tmp_file:
            tmp_path = Path(tmp_file.name)
            tmp_file.write(text)
            tmp_file.flush()
            os.fsync(tmp_file.fileno())
        assert tmp_path is not None
        tmp_path.replace(path)
    except OSError:
        if tmp_path is not None:
            with suppress(OSError):
                tmp_path.unlink(missing_ok=True)
        raise


def _remove_compose_environment_targets(
    payload: object,
    targets_by_service: Mapping[str, set[str]],
) -> int:
    if not isinstance(payload, dict):
        return 0
    services = payload.get("services")
    if not isinstance(services, dict):
        return 0

    removed_count = 0
    for service_name, targets in targets_by_service.items():
        service = services.get(service_name)
        if not isinstance(service, dict):
            continue
        environment = service.get("environment")
        if isinstance(environment, dict):
            for target in targets:
                if target in environment:
                    del environment[target]
                    removed_count += 1
            if not environment:
                del service["environment"]
            continue
        if isinstance(environment, list):
            retained_environment: list[object] = []
            for item in environment:
                if _compose_environment_list_item_targets(item, targets):
                    removed_count += 1
                    continue
                retained_environment.append(item)
            if len(retained_environment) != len(environment):
                if retained_environment:
                    service["environment"] = retained_environment
                else:
                    # Preserve list style so same-pass restores do not switch
                    # the section to Compose mapping form.
                    service["environment"] = []
    return removed_count


def _restore_compose_environment_refs(
    payload: object,
    refs_by_service: Mapping[str, Mapping[str, str]],
) -> int:
    if not isinstance(payload, dict):
        return 0
    services = payload.get("services")
    if not isinstance(services, dict):
        return 0

    restored_count = 0
    for service_name, refs in refs_by_service.items():
        if not refs:
            continue
        service = services.get(service_name)
        if not isinstance(service, dict):
            continue
        environment = service.get("environment")
        if isinstance(environment, dict):
            for target, ref in refs.items():
                if environment.get(target) != ref:
                    environment[target] = ref
                    restored_count += 1
            continue
        if isinstance(environment, list):
            restored_count += _restore_compose_environment_list_refs(environment, refs)
            continue
        if environment is None:
            service["environment"] = dict(refs)
            restored_count += len(refs)
    return restored_count


def _restore_compose_environment_list_refs(
    environment: list[object],
    refs: Mapping[str, str],
) -> int:
    restored_count = 0
    seen_targets: set[str] = set()
    restored_targets: set[str] = set()
    for index, item in enumerate(environment):
        if not isinstance(item, str):
            continue
        key = item.split("=", 1)[0]
        ref = refs.get(key)
        if ref is None:
            continue
        seen_targets.add(key)
        replacement = f"{key}={ref}"
        if item != replacement:
            environment[index] = replacement
            if key not in restored_targets:
                restored_targets.add(key)
                restored_count += 1

    for target, ref in refs.items():
        if target in seen_targets:
            continue
        environment.append(f"{target}={ref}")
        restored_count += 1
    return restored_count


def _compose_environment_list_item_targets(item: object, targets: set[str]) -> bool:
    if not isinstance(item, str):
        return False
    key = item.split("=", 1)[0]
    return key in targets
