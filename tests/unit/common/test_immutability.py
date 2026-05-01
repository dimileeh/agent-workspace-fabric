"""Read-only nested mapping helper tests."""

from __future__ import annotations

from typing import Any, cast

import pytest

from awf.common.immutability import frozen_mapping


@pytest.mark.unit
def test_frozen_mapping_rejects_all_mapping_mutators() -> None:
    frozen = cast(
        dict[str, Any],
        frozen_mapping({"items": ["a"], "labels": {"b", "a"}, "nested": {"value": 1}}),
    )

    mutators = (
        lambda: frozen.__delitem__("nested"),
        frozen.clear,
        lambda: frozen.pop("nested"),
        frozen.popitem,
        lambda: frozen.setdefault("other", 2),
        lambda: frozen.update({"other": 2}),
        lambda: frozen.__ior__({"other": 2}),
    )

    for mutate in mutators:
        with pytest.raises(TypeError, match="frozen mapping cannot be mutated"):
            mutate()

    with pytest.raises(TypeError, match="frozen mapping cannot be mutated"):
        frozen["other"] = 2


@pytest.mark.unit
def test_frozen_mapping_freezes_nested_sequences_and_sets() -> None:
    frozen = frozen_mapping({"items": ["a", {"b": 1}], "labels": {"z", "a"}})

    assert frozen["items"] == ["a", {"b": 1}]
    assert frozen["items"] == ("a", {"b": 1})
    assert sorted(frozen["labels"]) == ["a", "z"]
