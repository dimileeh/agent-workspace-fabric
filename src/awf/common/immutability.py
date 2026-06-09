"""Helpers for exposing read-only nested data."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, NoReturn, Self, cast


def frozen_mapping(value: Mapping[str, Any]) -> Mapping[str, Any]:
    """Return a recursively read-only mapping view for metadata-like values."""

    return cast(Mapping[str, Any], _freeze_value(value))


class _FrozenDict(dict[str, Any]):
    def __setitem__(self, key: str, value: Any) -> NoReturn:
        self._raise_frozen()

    def __delitem__(self, key: str) -> NoReturn:
        self._raise_frozen()

    def clear(self) -> NoReturn:
        self._raise_frozen()

    def pop(self, _key: str, _default: Any = None) -> NoReturn:
        self._raise_frozen()

    def popitem(self) -> NoReturn:
        self._raise_frozen()

    def setdefault(self, _key: str, _default: Any = None) -> NoReturn:
        self._raise_frozen()

    def update(self, *_args: Any, **_kwargs: Any) -> NoReturn:
        self._raise_frozen()

    def __ior__(self, value: object) -> Self:  # type: ignore[misc, override]
        self._raise_frozen()

    def _raise_frozen(self) -> NoReturn:
        raise TypeError("frozen mapping cannot be mutated")


class _FrozenSequence(tuple[Any, ...]):
    def __eq__(self, value: object) -> bool:
        if isinstance(value, list):
            return tuple(self) == tuple(value)
        return super().__eq__(value)


def _freeze_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return _FrozenDict({key: _freeze_value(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return _FrozenSequence(_freeze_value(item) for item in value)
    if isinstance(value, (set, frozenset)):
        return _FrozenSequence(_freeze_value(item) for item in value)
    return value
