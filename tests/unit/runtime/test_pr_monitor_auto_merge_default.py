"""Auto-merge default + create/adopt parity at the DECISION-OUTCOME level.

Monitor selection and the merge-vs-notify outcome are a pure function of the
resolved ``auto_merge`` flag. The flag itself is produced by the single shared
``resolve_auto_merge`` resolver that both create and adopt run at provision, so
identical (intent, profile, base_branch) inputs yield identical outcomes
regardless of which entry point created the workspace or what task_kind it has.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from awf.common.auto_merge import resolve_auto_merge
from awf.profiles.models import ProfileAutoMerge, ProfileMonitor, WorkspaceProfile
from awf.runtime.pr_monitor import (
    CheckState as _CheckState,
)
from awf.runtime.pr_monitor import (
    Merge,
    MergeableState,
    MergeStateStatus,
    MonitorConfig,
    MonitorState,
    NotifyHuman,
    PRStatus,
    decide,
)


def _green_status() -> PRStatus:
    """An all-green, mergeable PR that reaches the terminal merge gate."""
    return PRStatus(
        number=42,
        head_sha="abc123",
        mergeable=MergeableState.MERGEABLE,
        check_state=_CheckState.SUCCESS,
        unresolved_inline_threads=(),
        unresolved_review_comments=(),
        blocking_reviews=(),
        base_behind_count=0,
        merge_state_status=MergeStateStatus.CLEAN,
        ci_failures=(),
    )


def _profile(
    *, default: bool = False, by_base_branch: dict[str, bool] | None = None
) -> WorkspaceProfile:
    return WorkspaceProfile(
        name="p",
        monitor=ProfileMonitor(
            auto_merge=ProfileAutoMerge(default=default, by_base_branch=by_base_branch or {})
        ),
    )


@pytest.mark.unit
def test_monitor_config_default_auto_merge_is_false() -> None:
    # The decision-core default is off (closes the latent footgun).
    assert MonitorConfig().auto_merge is False


@pytest.mark.unit
@pytest.mark.parametrize(
    ("auto_merge", "expected"),
    [(True, Merge), (False, NotifyHuman)],
)
def test_resolved_flag_drives_merge_vs_notify(auto_merge: bool, expected: type) -> None:
    action = decide(_green_status(), MonitorState(), MonitorConfig(auto_merge=auto_merge))
    assert isinstance(action, expected)


@pytest.mark.unit
@pytest.mark.parametrize("intent", [True, False, None])
def test_default_off_intent_drives_merge_vs_notify(intent: bool | None) -> None:
    # Against a default-off profile the shared resolve_auto_merge (used verbatim by
    # both create and adopt at provision) maps each tri-state intent to a flag, which
    # drives the decide() outcome: True -> Merge; False/None (default off) -> NotifyHuman.
    profile = _profile(default=False)
    flag = resolve_auto_merge(intent, profile, "development")
    action = decide(_green_status(), MonitorState(), MonitorConfig(auto_merge=flag))
    assert isinstance(action, Merge if intent is True else NotifyHuman)


@pytest.mark.unit
def test_sync_release_default_notifies_but_explicit_auto_merge_merges() -> None:
    # task_kind no longer forces auto_merge; a defaulted (None-intent) sync_release
    # PR resolves off -> NotifyHuman on green, while an explicit --auto-merge
    # resolves on -> Merge. The resolver is identical to every other path.
    profile = _profile(default=False)
    default_flag = resolve_auto_merge(None, profile, "main")
    explicit_flag = resolve_auto_merge(True, profile, "main")
    assert isinstance(
        decide(_green_status(), MonitorState(), MonitorConfig(auto_merge=default_flag)),
        NotifyHuman,
    )
    assert isinstance(
        decide(_green_status(), MonitorState(), MonitorConfig(auto_merge=explicit_flag)),
        Merge,
    )


@pytest.mark.unit
@pytest.mark.parametrize("observed_base", ["main", None])
def test_retargeted_pr_does_not_merge_under_the_original_base_policy(
    observed_base: str | None,
) -> None:
    # auto_merge is resolved ONCE at provision time against the workspace's
    # branch_base (monitor.auto_merge.by_base_branch). A PR opened against an
    # auto-merging base and retargeted afterwards onto a human-gated one would
    # otherwise be merged under the stale base's policy, so a drifted base blocks.
    # ``base_ref`` is optional on the snapshot, so the hand-off message names the
    # new base when the forge reported one and stays generic when it did not.
    profile = _profile(default=False, by_base_branch={"development": True})
    flag = resolve_auto_merge(None, profile, "development")
    assert flag is True
    drifted = replace(_green_status(), base_ref=observed_base, base_ref_drifted=True)

    action = decide(drifted, MonitorState(), MonitorConfig(auto_merge=flag))

    assert isinstance(action, NotifyHuman)
    assert action.message is not None
    assert "base branch changed" in action.message
    assert ("main" in action.message) is (observed_base is not None)


@pytest.mark.unit
def test_unchanged_base_still_merges() -> None:
    # The drift gate must not fire for the ordinary case: the live base matches
    # the workspace's branch_base, so the runner leaves the flag off.
    flag = resolve_auto_merge(None, _profile(by_base_branch={"development": True}), "development")
    action = decide(
        replace(_green_status(), base_ref="development"),
        MonitorState(),
        MonitorConfig(auto_merge=flag),
    )
    assert isinstance(action, Merge)


@pytest.mark.unit
def test_adopt_already_green_pr_with_auto_merge_merges_immediately() -> None:
    # Guarding the adopt-an-already-green-PR path: an explicit auto_merge intent
    # resolves on and the first decide() on a green PR is a terminal Merge.
    flag = resolve_auto_merge(True, _profile(default=False), "development")
    action = decide(_green_status(), MonitorState(), MonitorConfig(auto_merge=flag))
    assert isinstance(action, Merge)
