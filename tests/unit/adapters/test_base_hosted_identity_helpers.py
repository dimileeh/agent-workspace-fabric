"""Hosted PR identity normalization helpers."""

import pytest

from awf.adapters.base import (
    _hosted_identity_int,
    _hosted_identity_str,
    _hosted_identity_str_tuple,
)


@pytest.mark.unit
@pytest.mark.parametrize(
    ("identity", "expected"),
    [
        (None, None),
        ({"repo_url": ""}, None),
        ({"repo_url": 123}, None),
        ({"repo_url": "https://github.example/repo"}, "https://github.example/repo"),
    ],
)
def test_hosted_identity_str_accepts_only_non_empty_strings(
    identity: dict[str, object] | None,
    expected: str | None,
) -> None:
    assert _hosted_identity_str(identity, "repo_url") == expected


@pytest.mark.unit
@pytest.mark.parametrize(
    ("identity", "expected"),
    [
        (None, None),
        ({"pr_number": True}, None),
        ({"pr_number": "42"}, None),
        ({"pr_number": 42}, 42),
    ],
)
def test_hosted_identity_int_rejects_bool_and_non_int_values(
    identity: dict[str, object] | None,
    expected: int | None,
) -> None:
    assert _hosted_identity_int(identity, "pr_number") == expected


@pytest.mark.unit
@pytest.mark.parametrize(
    ("identity", "expected"),
    [
        (None, ()),
        ({"owned_paths": "src/awf"}, ()),
        ({"owned_paths": ["src/awf", "", 7, "tests"]}, ("src/awf", "tests")),
        ({"owned_paths": ("docs", "README.md")}, ("docs", "README.md")),
    ],
)
def test_hosted_identity_str_tuple_keeps_only_non_empty_strings(
    identity: dict[str, object] | None,
    expected: tuple[str, ...],
) -> None:
    assert _hosted_identity_str_tuple(identity, "owned_paths") == expected
