"""Tests for ``merge_agent_environment`` git-config re-indexing semantics."""

from __future__ import annotations

import pytest

from awf.profiles.compose import merge_agent_environment


@pytest.mark.unit
def test_merge_environment_does_not_overwrite_existing_keys() -> None:
    assert merge_agent_environment(
        (("EXISTING", "base"),),
        (("EXISTING", "override"), ("NEW", "value")),
    ) == (("EXISTING", "base"), ("NEW", "value"))


def _git_config_pairs(env: dict[str, str]) -> set[tuple[str, str]]:
    count = int(env["GIT_CONFIG_COUNT"])
    return {(env[f"GIT_CONFIG_KEY_{i}"], env[f"GIT_CONFIG_VALUE_{i}"]) for i in range(count)}


@pytest.mark.unit
def test_merge_environment_reindexes_git_config_when_base_presets_count() -> None:
    # A profile that already declares indexed GIT_CONFIG_* in runtime.environment
    # presets GIT_CONFIG_COUNT. The bitbucket lease resolver always emits its
    # insteadOf entries starting at index 0; a naive "skip existing keys" merge
    # would keep the profile's lower count and drop/orphan the lease entries so
    # they never reach git (broken private Bitbucket HTTPS). The merge must
    # re-index the additions on top of the base count and sum the totals so BOTH
    # blocks survive contiguously.
    base = (
        ("GIT_CONFIG_COUNT", "1"),
        ("GIT_CONFIG_KEY_0", "user.name"),
        ("GIT_CONFIG_VALUE_0", "Profile Bot"),
    )
    additions = (
        ("BITBUCKET_API_TOKEN", "${BITBUCKET_API_TOKEN}"),
        ("GIT_CONFIG_COUNT", "2"),
        ("GIT_CONFIG_KEY_0", "url.https://x@bitbucket.org/.insteadOf"),
        ("GIT_CONFIG_VALUE_0", "https://bitbucket.org/"),
        ("GIT_CONFIG_KEY_1", "url.https://x@bitbucket.org/.insteadOf"),
        ("GIT_CONFIG_VALUE_1", "git@bitbucket.org:"),
    )

    merged = dict(merge_agent_environment(base, additions))

    assert merged["GIT_CONFIG_COUNT"] == "3"
    assert merged["BITBUCKET_API_TOKEN"] == "${BITBUCKET_API_TOKEN}"
    pairs = _git_config_pairs(merged)
    # The profile entry survives unchanged...
    assert ("user.name", "Profile Bot") in pairs
    # ...and both lease entries are reachable within GIT_CONFIG_COUNT.
    assert ("url.https://x@bitbucket.org/.insteadOf", "https://bitbucket.org/") in pairs
    assert ("url.https://x@bitbucket.org/.insteadOf", "git@bitbucket.org:") in pairs
    # No stray protocol key sits above the effective count.
    assert "GIT_CONFIG_KEY_3" not in merged


@pytest.mark.unit
def test_merge_environment_tolerates_non_integer_base_git_config_count() -> None:
    # A malformed (non-integer) profile GIT_CONFIG_COUNT must not crash the merge;
    # it is treated as zero so the lease entries still land contiguously.
    base = (("GIT_CONFIG_COUNT", "not-a-number"),)
    additions = (
        ("GIT_CONFIG_COUNT", "1"),
        ("GIT_CONFIG_KEY_0", "url.https://x@bitbucket.org/.insteadOf"),
        ("GIT_CONFIG_VALUE_0", "https://bitbucket.org/"),
    )

    merged = dict(merge_agent_environment(base, additions))

    assert merged["GIT_CONFIG_COUNT"] == "1"
    assert _git_config_pairs(merged) == {
        ("url.https://x@bitbucket.org/.insteadOf", "https://bitbucket.org/")
    }


@pytest.mark.unit
def test_merge_environment_skips_additions_index_gap_above_real_entries() -> None:
    # An additions count that overstates the present entries must not fabricate
    # empty (None) entries; the missing index is skipped, not re-emitted.
    additions = (
        ("GIT_CONFIG_COUNT", "2"),
        ("GIT_CONFIG_KEY_0", "url.https://x@bitbucket.org/.insteadOf"),
        ("GIT_CONFIG_VALUE_0", "https://bitbucket.org/"),
        # index 1 declared by the count but with no KEY/VALUE pair present.
    )

    merged = dict(merge_agent_environment((), additions))

    assert merged["GIT_CONFIG_COUNT"] == "1"
    assert _git_config_pairs(merged) == {
        ("url.https://x@bitbucket.org/.insteadOf", "https://bitbucket.org/")
    }


@pytest.mark.unit
def test_merge_environment_normalizes_base_count_overstating_entries() -> None:
    # A profile base whose GIT_CONFIG_COUNT overstates the present entries (holes
    # at indices 1..N-1) must not leave those holes in the merged block: git
    # rejects a block whose count exceeds the contiguous entries, so the lease
    # insteadOf rules would never reach git. The base is normalized to its
    # present entries and the lease entries land contiguously above it.
    base = (
        ("GIT_CONFIG_COUNT", "5"),
        ("GIT_CONFIG_KEY_0", "user.name"),
        ("GIT_CONFIG_VALUE_0", "Profile Bot"),
        # indices 1..4 declared by the count but absent.
    )
    additions = (
        ("GIT_CONFIG_COUNT", "1"),
        ("GIT_CONFIG_KEY_0", "url.https://x@bitbucket.org/.insteadOf"),
        ("GIT_CONFIG_VALUE_0", "https://bitbucket.org/"),
    )

    merged = dict(merge_agent_environment(base, additions))

    # Count matches the present entries (no holes) and _git_config_pairs (which
    # reads every index < count) does not raise on a missing index.
    assert merged["GIT_CONFIG_COUNT"] == "2"
    assert _git_config_pairs(merged) == {
        ("user.name", "Profile Bot"),
        ("url.https://x@bitbucket.org/.insteadOf", "https://bitbucket.org/"),
    }
    assert "GIT_CONFIG_KEY_2" not in merged


@pytest.mark.unit
def test_merge_environment_normalizes_base_indexed_keys_without_count() -> None:
    # A profile base with indexed GIT_CONFIG_KEY_n entries but no GIT_CONFIG_COUNT
    # must not collide with the lease entries (which start at index 0) nor leave a
    # mismatched count. Orphan keys without a count never reach git, so dropping
    # them while the lease entries claim a clean 0-based block is correct.
    base = (
        ("GIT_CONFIG_KEY_0", "user.name"),
        ("GIT_CONFIG_VALUE_0", "Profile Bot"),
    )
    additions = (
        ("GIT_CONFIG_COUNT", "1"),
        ("GIT_CONFIG_KEY_0", "url.https://x@bitbucket.org/.insteadOf"),
        ("GIT_CONFIG_VALUE_0", "https://bitbucket.org/"),
    )

    merged = dict(merge_agent_environment(base, additions))

    assert merged["GIT_CONFIG_COUNT"] == "1"
    assert _git_config_pairs(merged) == {
        ("url.https://x@bitbucket.org/.insteadOf", "https://bitbucket.org/")
    }


@pytest.mark.unit
def test_merge_environment_preserves_non_indexed_git_config_prefixed_keys() -> None:
    # ``GIT_CONFIG_KEY_n``/``GIT_CONFIG_VALUE_n`` is git's numerically-indexed
    # protocol. A profile env var that merely shares the prefix but carries a
    # non-numeric suffix (e.g. ``GIT_CONFIG_KEY_THRESHOLD``) is NOT a protocol
    # entry: it must flow through the regular first-writer-wins merge. The old
    # broad prefix match stripped it from ``others`` yet, lacking a numeric index,
    # never re-emitted it — silently dropping the variable from the container.
    base = (
        ("GIT_CONFIG_KEY_THRESHOLD", "5"),
        ("GIT_CONFIG_VALUE_CUSTOM", "x"),
    )
    additions = (
        ("GIT_CONFIG_COUNT", "1"),
        ("GIT_CONFIG_KEY_0", "url.https://x@bitbucket.org/.insteadOf"),
        ("GIT_CONFIG_VALUE_0", "https://bitbucket.org/"),
    )

    merged = dict(merge_agent_environment(base, additions))

    # The non-protocol vars survive unchanged...
    assert merged["GIT_CONFIG_KEY_THRESHOLD"] == "5"
    assert merged["GIT_CONFIG_VALUE_CUSTOM"] == "x"
    # ...and the genuine indexed lease block is still re-emitted contiguously.
    assert merged["GIT_CONFIG_COUNT"] == "1"
    assert _git_config_pairs(merged) == {
        ("url.https://x@bitbucket.org/.insteadOf", "https://bitbucket.org/")
    }


@pytest.mark.unit
def test_merge_environment_appends_git_config_when_base_has_none() -> None:
    # When the base carries no git-config protocol vars the additions keep their
    # own contiguous 0-based indexing (unchanged from the pre-fix behavior).
    base = (("PYTHONUNBUFFERED", "1"),)
    additions = (
        ("GIT_CONFIG_COUNT", "1"),
        ("GIT_CONFIG_KEY_0", "url.https://x@bitbucket.org/.insteadOf"),
        ("GIT_CONFIG_VALUE_0", "https://bitbucket.org/"),
    )

    merged = dict(merge_agent_environment(base, additions))

    assert merged["GIT_CONFIG_COUNT"] == "1"
    assert _git_config_pairs(merged) == {
        ("url.https://x@bitbucket.org/.insteadOf", "https://bitbucket.org/")
    }
