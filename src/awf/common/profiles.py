"""Shared profile validation error scrubbing and formatting helpers."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

__all__ = (
    "format_safe_validation_location",
    "format_safe_validation_message",
    "is_allowlisted_validation_message",
)

_SAFE_PYDANTIC_ERROR_TYPES: set[str] = {
    "missing",
    "extra_forbidden",
    "string_type",
    "int_type",
    "float_type",
    "number_type",
    "bool_type",
    "list_type",
    "dict_type",
    "tuple_type",
    "set_type",
    "none_required",
    "string_too_short",
    "string_too_long",
    "string_pattern_mismatch",
    "greater_than",
    "greater_than_equal",
    "less_than",
    "less_than_equal",
    "multiple_of",
    "enum",
    "literal_error",
    "uuid_type",
    "url_type",
    "json_type",
    "model_type",
}

_ALLOWLISTED_STATIC_MESSAGES: set[str] = {
    "Value error",
    "Assertion error",
    "Validation error",
    "schema validation failed",
    "healthcheck must set exactly one of command or url",
    "healthcheck kind must match command/url configuration",
    "healthcheck url must be an absolute http or https URL",
    "healthcheck must set command or url",
    "runtime.toolchains must be a mapping of language to versions",
    "runtime.toolchains language keys must be strings",
    "runtime.browsers must be a list of browser names",
    "runtime.browsers entries must be strings",
    "service must set either image or build_context",
    "service cannot set both image and build_context",
    "registry host must not be empty",
    "registry host URL must be absolute http or https",
    "registry host URL must not include credentials",
    "registry host must be a hostname or URL without credentials",
    "registry host must include a hostname",
    "registry host must be a valid hostname",
    "invalid toolchain language identifier",
    "invalid runtime browser",
    "profile service path must be workspace-relative",
    "profile service path escapes workspace root",
    "timestamp must be UTC-aware",
}


def is_allowlisted_validation_message(msg: str) -> bool:
    """Check if a validation message is an allowlisted safe static message."""
    if msg in _ALLOWLISTED_STATIC_MESSAGES:
        return True
    return bool(
        msg.endswith(" must be a workspace-relative path")
        or msg.endswith(" must include '{workspace_id}'")
        or msg.endswith(" must be a URL path")
    )


def format_safe_validation_message(err: Mapping[str, Any]) -> str:
    """Format a safe validation error message, converting custom validator msg strings to allowlisted messages."""
    err_type = str(err.get("type", ""))
    raw_msg = str(err.get("msg", "Validation error"))

    clean_msg = raw_msg
    if clean_msg.startswith("Value error, "):
        clean_msg = clean_msg[len("Value error, ") :]
    elif clean_msg.startswith("Assertion failed, "):
        clean_msg = clean_msg[len("Assertion failed, ") :]

    if err_type in _SAFE_PYDANTIC_ERROR_TYPES:
        return raw_msg

    if is_allowlisted_validation_message(clean_msg):
        return clean_msg

    if err_type == "value_error" or raw_msg.startswith("Value error"):
        return "Value error"
    if err_type == "assertion_error" or raw_msg.startswith("Assertion failed"):
        return "Assertion error"

    return "Validation error"


_ALLOWLISTED_FIELD_NAMES: set[str] = {
    "agent",
    "agent_image",
    "allow_hosts",
    "app_endpoints",
    "artifacts",
    "auto_merge",
    "awf",
    "base_branch",
    "browsers",
    "build_context",
    "by_base_branch",
    "command",
    "commit_message",
    "compose_env_file",
    "cost_per_unit",
    "coverage",
    "currency",
    "custom",
    "database",
    "default",
    "details",
    "docker",
    "effort",
    "egress",
    "environment",
    "error",
    "execution",
    "extra_secrets",
    "filter",
    "forge",
    "healthcheck",
    "idempotency_key",
    "image",
    "initial_review_grace_period_seconds",
    "inline_profile",
    "interval_seconds",
    "kind",
    "labels",
    "limit",
    "logs",
    "mode",
    "model",
    "monitor",
    "name",
    "offset",
    "operation_type",
    "out_of_scope_change_policy",
    "owned_paths",
    "payload",
    "phases",
    "planning",
    "ports",
    "pr_number",
    "pr_url",
    "pricing",
    "profile",
    "profile_ref",
    "pull_request_number",
    "quality",
    "reason",
    "report_path",
    "repo_slug",
    "repo_url",
    "retries",
    "runtime",
    "secrets",
    "security",
    "services",
    "settings",
    "source",
    "start_period_seconds",
    "status",
    "target_branch",
    "target_percent",
    "task_class",
    "task_kind",
    "task_prompt",
    "task_tag",
    "task_title",
    "test_results",
    "timeout_seconds",
    "toolchain_image",
    "toolchains",
    "type",
    "unit",
    "url",
    "validation",
    "validation_commands",
    "version",
    "volumes",
    "workspace_id",
}

_DYNAMIC_MAPPING_FIELDS: set[str] = {
    "by_base_branch",
    "custom",
    "details",
    "environment",
    "extra_env",
    "headers",
    "labels",
    "ports",
    "toolchains",
    "variables",
}


def format_safe_validation_location(loc: Any) -> str:
    """Format a validation location tuple, replacing dynamic mapping keys or unallowlisted segments with `<key>`."""
    if not loc:
        return ""
    if isinstance(loc, (str, int)):
        loc_tuple = (loc,)
    elif isinstance(loc, (tuple, list)):
        loc_tuple = tuple(loc)
    else:
        loc_tuple = (str(loc),)

    formatted: list[str] = []
    prev_field: str | None = None

    for part in loc_tuple:
        if prev_field in _DYNAMIC_MAPPING_FIELDS:
            formatted.append("<key>")
            prev_field = None
        elif isinstance(part, int) or (isinstance(part, str) and part.isdigit()):
            formatted.append(str(part))
            prev_field = None
        else:
            part_str = str(part)
            if part_str in _ALLOWLISTED_FIELD_NAMES:
                formatted.append(part_str)
                prev_field = part_str
            else:
                formatted.append("<key>")
                prev_field = None

    return ".".join(formatted)
