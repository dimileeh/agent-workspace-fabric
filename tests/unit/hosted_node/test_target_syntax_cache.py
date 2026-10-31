"""Only pure target syntax is reusable; every binding still validates authority."""

from datetime import datetime
from uuid import uuid4

import pytest
from pydantic import ValidationError

from awf.hosted_node import schemas
from tests.unit.test_hosted_runtime_contract import hosted_create


def value(target):
    binding = hosted_create()["hosted"]["credentials"]["agent"]
    return {**binding, "target": target}


def test_repeated_target_syntax_is_checked_once_without_reusing_the_binding(monkeypatch):
    original = schemas.PurePosixPath
    calls = []

    def observed(target):
        calls.append(target)
        return original(target)

    monkeypatch.setattr(schemas, "PurePosixPath", observed)
    target = "/run/awf/secrets/" + uuid4().hex
    for fence in range(1, 21):
        result = schemas.Binding.model_validate({**value(target), "fence": fence})
        assert result.target == target and result.fence == fence
    assert calls == [target]


def test_target_syntax_cache_has_a_bounded_exact_key(monkeypatch):
    original = schemas.PurePosixPath
    calls = []

    def observed(target):
        calls.append(target)
        return original(target)

    monkeypatch.setattr(schemas, "PurePosixPath", observed)
    prefix = "/run/awf/secrets/" + uuid4().hex
    targets = [f"{prefix}-{index}" for index in range(257)]
    for target in targets:
        schemas.Binding.model_validate(value(target))
    assert calls == targets
    schemas.Binding.model_validate(value(targets[-1]))
    assert calls == targets
    schemas.Binding.model_validate(value(targets[0]))
    assert calls == [*targets, targets[0]]


@pytest.mark.parametrize(
    "target",
    [
        "/run/awf/secrets/../key",
        "/run/awf/secrets//key",
        "/run/awf/secrets/./key",
        "/run/awf/secrets/key/",
        "/tmp/key",
        "/run/awf/secrets/key\n",
        "/run/awf/secrets/\u00e9",
    ],
)
def test_valid_target_cache_does_not_accept_changed_or_repeated_invalid_target(target):
    schemas.Binding.model_validate(value("/run/awf/secrets/key"))
    for _ in range(2):
        with pytest.raises(ValidationError, match="INVALID_TARGET"):
            schemas.Binding.model_validate(value(target))


@pytest.mark.parametrize("field", ["identity", "fence", "revision", "expires_at", "purpose"])
def test_cached_target_does_not_skip_other_binding_validation(field):
    binding = value("/run/awf/secrets/key")
    schemas.Binding.model_validate(binding)
    invalid = {
        "identity": {**binding["identity"], "boot_epoch": 0},
        "fence": 0,
        "revision": 0,
        "expires_at": datetime(2026, 1, 1),
        "purpose": "untrusted",
    }
    with pytest.raises(ValidationError):
        schemas.Binding.model_validate({**binding, field: invalid[field]})
