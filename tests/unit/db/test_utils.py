from __future__ import annotations

import pytest

from awf.db.utils import escape_like_pattern


@pytest.mark.unit
@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("plain", "plain"),
        ("adopt_%:key", "adopt\\_\\%:key"),
        ("path\\with\\slashes", "path\\\\with\\\\slashes"),
        ("mixed\\_%", "mixed\\\\\\_\\%"),
    ],
)
def test_escape_like_pattern_escapes_like_wildcards(value: str, expected: str) -> None:
    assert escape_like_pattern(value) == expected
