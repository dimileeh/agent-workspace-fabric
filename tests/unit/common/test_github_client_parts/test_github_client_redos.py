"""ReDoS hardening regressions for ``RepoRef.from_url`` bare-slug parsing.

CodeQL alert (``py/redos``) flagged the bare ``owner/repo`` slug regex in
``RepoRef.from_url`` for catastrophic backtracking: the lazy ``([^/\\s]+?)``
group immediately followed by ``(?:\\.git)?`` (whose ``.git`` characters are
themselves in ``[^/\\s]``) gave a quote/dot-heavy tail exponentially many ways
to split. These tests pin the *exact* accept/reject set and parsed owner/name
against results captured once from the original (pre-hardening) pattern, and
bound the runtime on a pathological input so the backtracking cannot return.

The expected owner/name pairs below were captured once from the pre-hardening
patterns. We bake the expected results as literals rather than re-compiling the
original (vulnerable) regexes here: re-shipping those literals would itself be a
``py/redos`` finding, and the differential guard is just as strong asserting the
hardened parser against the captured pairs.
"""

from __future__ import annotations

import time

import pytest

from awf.common.github_client import RepoRef

# (input, expected (owner, name)) captured once from the ORIGINAL pre-hardening
# bare-slug regex ``([^/\s]+)/([^/\s]+?)(?:\.git)?/?``. Every corpus entry is a
# valid bare slug, so the expected value is never ``None``.
_BARE_SLUG_CASES: list[tuple[str, tuple[str, str]]] = [
    ("owner/repo", ("owner", "repo")),
    ("owner/repo.git", ("owner", "repo")),
    ("owner/repo/", ("owner", "repo")),
    ("owner/repo.git/", ("owner", "repo")),
    ("owner/repo.git.git", ("owner", "repo.git")),
    ("owner/.git", ("owner", ".git")),
    ("owner/repo.github", ("owner", "repo.github")),
    ("o/r", ("o", "r")),
    ("o/r.git", ("o", "r")),
    ("owner/repo.gitfoo", ("owner", "repo.gitfoo")),
    ("owner/.gitignore", ("owner", ".gitignore")),
    ("dimileeh/aira-web", ("dimileeh", "aira-web")),
    ("dimileeh/aira-web.git", ("dimileeh", "aira-web")),
]


@pytest.mark.unit
@pytest.mark.parametrize("value, expected", _BARE_SLUG_CASES)
def test_bare_slug_owner_name_matches_original(value: str, expected: tuple[str, str]) -> None:
    """Hardened parsing yields the same owner/name the original regex produced."""
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
    """A long ``.git``-overlap tail with a non-matching terminator parses linearly.

    Regression for CodeQL ``py/redos`` on ``github_client.py:165``. The original
    lazy-group + ``(?:\\.git)?`` overlap is the backtracking anti-pattern CodeQL
    flagged; the hardened possessive form has a single unambiguous match path
    and completes in microseconds.

    The input must actually exercise that overlap to pin the fix: it repeats real
    ``.git`` fragments (so the lazy group and the optional ``(?:\\.git)?`` fight
    over every fragment boundary) and ends in a non-matching ``/x`` terminator,
    forcing the *failure* path where a re-introduced lazy pattern backtracks. A
    bare ``b.g``-style tail never contains the ``.git`` suffix and parses as a
    trivial success under either pattern, so it would not catch a regression.

    The 5.0s budget is far above any realistic parse time (so a loaded/
    memory-constrained CI runner won't fail spuriously) yet far below the
    seconds-to-minutes a re-introduced backtracking regression would take.
    """
    pathological = "a" * 5000 + "/" + "b.git" * 2000 + "/x"
    start = time.perf_counter()
    with pytest.raises(ValueError):
        RepoRef.from_url(pathological)
    assert time.perf_counter() - start < 5.0


# (input, expected (owner, name)) captured once from the ORIGINAL pre-hardening
# SSH scp-like regex ``git@github\.com:([^/]+)/([^/]+?)(?:\.git)?/?``. Every
# corpus entry is a valid SSH ref, so the expected value is never ``None``.
_SSH_CASES: list[tuple[str, tuple[str, str]]] = [
    ("git@github.com:owner/repo", ("owner", "repo")),
    ("git@github.com:owner/repo.git", ("owner", "repo")),
    ("git@github.com:owner/repo/", ("owner", "repo")),
    ("git@github.com:owner/repo.git/", ("owner", "repo")),
    ("git@github.com:owner/repo.git.git", ("owner", "repo.git")),
    ("git@github.com:owner/.git", ("owner", ".git")),
    ("git@github.com:owner/repo.github", ("owner", "repo.github")),
    ("git@github.com:o/r", ("o", "r")),
    ("git@github.com:o/r.git", ("o", "r")),
    ("git@github.com:dimileeh/aira-web", ("dimileeh", "aira-web")),
    ("git@github.com:dimileeh/aira-web.git", ("dimileeh", "aira-web")),
]


@pytest.mark.unit
@pytest.mark.parametrize("value, expected", _SSH_CASES)
def test_ssh_owner_name_matches_original(value: str, expected: tuple[str, str]) -> None:
    """Hardened SSH parsing yields the same owner/name the original regex produced."""
    ref = RepoRef.from_url(value)
    assert (ref.owner, ref.name) == expected
    assert ref.forge == "github"


@pytest.mark.unit
def test_ssh_pathological_tail_is_linear() -> None:
    """A pathological SSH ``.git``-overlap tail parses linearly.

    Regression for the SSH scp-like form on ``github_client.py:198``, which shared
    the same lazy ``([^/]+?)`` + ``(?:\\.git)?`` overlap CodeQL flagged on the
    bare-slug path. The tail repeats real ``.git`` fragments (so the lazy group and
    the optional ``(?:\\.git)?`` fight over every fragment boundary) and ends in a
    trailing ``/x`` the name group cannot consume, forcing the *failure* path where
    a re-introduced lazy pattern backtracks. The hardened possessive form has a
    single match path and completes in microseconds. The 5.0s budget mirrors the
    bare-slug guard: well above any real parse time, well below a regression's.
    """
    pathological = "git@github.com:owner/" + "b.git" * 4000 + "/x"
    start = time.perf_counter()
    with pytest.raises(ValueError):
        RepoRef.from_url(pathological)
    assert time.perf_counter() - start < 5.0
