"""ReDoS hardening regressions for ``RepoRef.from_url`` bare-slug parsing.

CodeQL alert (``py/redos``) flagged the bare ``owner/repo`` slug regex in
``RepoRef.from_url`` for catastrophic backtracking: the lazy ``([^/\\s]+?)``
group immediately followed by ``(?:\\.git)?`` (whose ``.git`` characters are
themselves in ``[^/\\s]``) gave a quote/dot-heavy tail exponentially many ways
to split. These tests pin the *exact* accept/reject set and parsed owner/name
against an embedded copy of the original (pre-hardening) pattern, and bound the
runtime on a pathological input so the backtracking cannot return.
"""

from __future__ import annotations

import re
import time

import pytest

from awf.common.github_client import RepoRef

# Embedded copy of the ORIGINAL (pre-hardening) bare-slug regex. Kept local to
# the test (not imported) so the differential guard proves equivalence without
# shipping the vulnerable pattern in production code.
_ORIGINAL_BARE_SLUG_RE = re.compile(r"([^/\s]+)/([^/\s]+?)(?:\.git)?/?")


def _original_parse(value: str) -> tuple[str, str] | None:
    """Replicate the original bare-slug owner/name extraction."""
    match = _ORIGINAL_BARE_SLUG_RE.fullmatch(value)
    if match is None:
        return None
    return match.group(1), match.group(2)


_BARE_SLUG_CASES = [
    "owner/repo",
    "owner/repo.git",
    "owner/repo/",
    "owner/repo.git/",
    "owner/repo.git.git",
    "owner/.git",
    "owner/repo.github",
    "o/r",
    "o/r.git",
    "owner/repo.gitfoo",
    "owner/.gitignore",
    "dimileeh/aira-web",
    "dimileeh/aira-web.git",
]


@pytest.mark.unit
@pytest.mark.parametrize("value", _BARE_SLUG_CASES)
def test_bare_slug_owner_name_matches_original(value: str) -> None:
    """Hardened parsing yields the same owner/name the original regex produced."""
    expected = _original_parse(value)
    assert expected is not None, "test corpus entry must be a valid bare slug"
    ref = RepoRef.from_url(value)
    assert (ref.owner, ref.name) == expected
    assert ref.forge == "github"


@pytest.mark.unit
@pytest.mark.parametrize(
    "value, expected_name",
    [
        ("owner/repo", "repo"),
        ("owner/repo.git", "repo"),
        ("owner/repo.git/", "repo"),
        ("owner/repo.git.git", "repo.git"),
        ("owner/.git", ".git"),  # len == 4: trailing ``.git`` is NOT stripped
        ("owner/repo.github", "repo.github"),
    ],
)
def test_bare_slug_git_suffix_strip_edges(value: str, expected_name: str) -> None:
    """The ``.git`` strip replicates the original lazy behavior exactly."""
    assert RepoRef.from_url(value).name == expected_name


@pytest.mark.unit
def test_bare_slug_pathological_tail_is_linear() -> None:
    """A long dot/``.git``-heavy tail must parse in linear time.

    Regression for CodeQL ``py/redos`` on ``github_client.py:165``. The original
    lazy-group + ``(?:\\.git)?`` overlap is the backtracking anti-pattern CodeQL
    flagged; the hardened possessive form has a single unambiguous match path
    and completes well under the 1.0s budget. This guards against the ambiguity
    being re-introduced.
    """
    pathological = "a" * 5000 + "/" + "b.g" * 2000
    start = time.perf_counter()
    RepoRef.from_url(pathological)
    assert time.perf_counter() - start < 1.0
