"""ASCII quote classification shared by source-presence scanners.

The module keeps its historical name to avoid an unrelated rename in the
verdict-protocol simplification. It no longer contains verdict segmentation or
Markdown-aware delimiter state.
"""

from __future__ import annotations

_ASCII_APOSTROPHE_CONTRACTION_SUFFIXES = frozenset({"t", "s", "re", "ve", "ll", "d", "m"})
_ASCII_LEADING_ELISION_SUFFIXES = frozenset(
    {
        "em",
        "tis",
        "twas",
        "twere",
        "twill",
        "twould",
        "twixt",
        "til",
        "till",
        "cause",
        "bout",
        "round",
        "nother",
        "cept",
        "gainst",
        "fore",
        "stead",
        "cross",
        "neath",
        "ere",
    }
)


def _ascii_quote_is_backslash_escaped(text: str, index: int) -> bool:
    """Return whether ``text[index]`` follows an odd backslash run."""
    count = 0
    cursor = index - 1
    while cursor >= 0 and text[cursor] == "\\":
        count += 1
        cursor -= 1
    return count % 2 == 1


def _ascii_double_quote_is_delimiter(text: str, index: int, inside_quote: bool) -> bool:
    """Distinguish double-quote delimiters from escaped quotes and unit marks."""
    if _ascii_quote_is_backslash_escaped(text, index):
        return False
    if inside_quote:
        return True
    previous = text[index - 1] if index > 0 else ""
    return not previous.isalnum()


def _ascii_single_quote_is_delimiter(text: str, index: int, inside_quote: bool) -> bool:
    """Distinguish single-quote delimiters from apostrophes and elisions."""
    if _ascii_quote_is_backslash_escaped(text, index):
        return False
    previous = text[index - 1] if index > 0 else ""
    following = text[index + 1] if index + 1 < len(text) else ""
    if previous.isalnum() and following.islower():
        return inside_quote and not _ascii_apostrophe_is_contraction_suffix(text, index)
    if _ascii_apostrophe_is_leading_elision(text, index):
        return False
    return inside_quote or not previous.isalnum()


def _ascii_apostrophe_is_contraction_suffix(text: str, index: int) -> bool:
    """Return whether letters after the apostrophe form a short contraction."""
    end = index + 1
    while end < len(text) and text[end].isalpha():
        end += 1
    return text[index + 1 : end].lower() in _ASCII_APOSTROPHE_CONTRACTION_SUFFIXES


def _ascii_apostrophe_is_leading_elision(text: str, index: int) -> bool:
    """Return whether the apostrophe begins a known leading elision."""
    previous = text[index - 1] if index > 0 else ""
    if previous.isalnum():
        return False
    end = index + 1
    while end < len(text) and text[end].isalpha():
        end += 1
    return text[index + 1 : end].lower() in _ASCII_LEADING_ELISION_SUFFIXES
