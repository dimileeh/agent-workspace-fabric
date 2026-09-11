"""Malformed hosted-adoption identity regressions for workspace retry."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from awf.common.forge_lifecycle import PullRequestLifecycle
from awf.service import workspaces_retry_feature_pr

pytestmark = pytest.mark.unit


def _valid_hosted_adoption_source() -> SimpleNamespace:
    return SimpleNamespace(
        task_kind="sync_feature_pr",
        repo_url="git@github.com:example/retryable.git",
        pr_number=42,
        pr_url="https://github.com/example/retryable/pull/42",
        resolved_profile={"source": "retry-test-profile"},
        task_policy={
            "pr_adoption": {
                "repo_slug": "example/retryable",
                "pr_number": 42,
                "pr_url": "https://github.com/example/retryable/pull/42",
                "head_ref": "contributors/fix-123",
                "base_ref": "main",
                "head_sha": "b" * 40,
                "base_sha": "a" * 40,
                "execution": {"mode": "hosted"},
            }
        },
    )


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        pytest.param(True, None, id="bool-is-not-an-int-identity"),
        pytest.param(" 42 ", 42, id="positive-digit-string"),
        pytest.param("0", None, id="non-positive-digit-string"),
        pytest.param("not-a-number", None, id="non-digit-string"),
    ],
)
def test_adoption_pr_number_accepts_only_positive_unambiguous_values(
    raw: object,
    expected: int | None,
) -> None:
    """Ambiguous values must not be treated as trusted PR identity."""
    assert (
        workspaces_retry_feature_pr._adoption_identity_pr_number(  # noqa: SLF001
            {"pr_number": raw}
        )
        == expected
    )


@pytest.mark.parametrize(
    ("component", "value"),
    [
        pytest.param("base_sha", "short", id="abbreviated-base-sha"),
        pytest.param("head_sha", "short", id="abbreviated-head-sha"),
        pytest.param("pr_number", True, id="ambiguous-adoption-pr-number"),
        pytest.param("source_pr_number", 99, id="conflicting-workspace-pr-number"),
        pytest.param("source_repo_url", "not-a-repository", id="invalid-source-repository"),
        pytest.param(
            "pr_url",
            "https://github.com/example/retryable/pull/99",
            id="conflicting-adoption-url-number",
        ),
        pytest.param("source_pr_url", "not-a-pull-request-url", id="invalid-workspace-url"),
        pytest.param(
            "source_pr_url",
            "https://github.com/example/retryable/pull/99",
            id="conflicting-workspace-url-number",
        ),
    ],
)
def test_hosted_adoption_rejects_malformed_or_conflicting_identity_components(
    component: str,
    value: object,
) -> None:
    """Untrusted identity components cannot qualify the local-auth bypass."""
    source = _valid_hosted_adoption_source()
    adoption = source.task_policy["pr_adoption"]
    if component == "source_pr_number":
        source.pr_number = value
    elif component == "source_pr_url":
        source.pr_url = value
    elif component == "source_repo_url":
        source.repo_url = value
    else:
        adoption[component] = value

    prefetched = workspaces_retry_feature_pr._PrefetchedFeaturePrState(  # noqa: SLF001
        pr_number=42,
        lifecycle=PullRequestLifecycle.open,
    )

    assert not (
        workspaces_retry_feature_pr._is_retained_open_hosted_pr_adoption_retry(  # noqa: SLF001
            source,  # type: ignore[arg-type]
            prefetched,
        )
    )
