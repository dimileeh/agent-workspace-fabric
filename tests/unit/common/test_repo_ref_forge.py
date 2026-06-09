"""Forge-aware ``RepoRef`` parsing + URL builders (issue #345 Phase 1).

GitHub behavior is unchanged; these tests add Bitbucket host detection and
host-aware URL builders. The existing GitHub ``RepoRef`` suite in
``test_github_client_parts`` must stay green alongside these.
"""

from __future__ import annotations

import pytest

from awf.common.github_client import RepoRef


class TestRepoRefForgeDetection:
    @pytest.mark.unit
    @pytest.mark.parametrize(
        "url",
        [
            "dimileeh/aira-web",
            "git@github.com:dimileeh/aira-web.git",
            "git@github.com:dimileeh/aira-web",
            "ssh://git@github.com/dimileeh/aira-web.git",
            "https://github.com/dimileeh/aira-agent.git",
            "https://x-access-token:tok@github.com/dimileeh/aira-agent.git",
            "https://github.com/dimileeh/aira-agent/",
            "https://github.com/dimileeh/aira-agent",
        ],
    )
    def test_github_urls_default_to_github_forge(self, url: str) -> None:
        assert RepoRef.from_url(url).forge == "github"

    @pytest.mark.unit
    @pytest.mark.parametrize(
        "url, expected_slug",
        [
            ("git@bitbucket.org:workspace/repo.git", "workspace/repo"),
            ("git@bitbucket.org:workspace/repo", "workspace/repo"),
            ("https://bitbucket.org/workspace/repo.git", "workspace/repo"),
            ("https://bitbucket.org/workspace/repo", "workspace/repo"),
            ("https://bitbucket.org/workspace/repo/", "workspace/repo"),
            ("ssh://git@bitbucket.org/workspace/repo.git", "workspace/repo"),
        ],
    )
    def test_bitbucket_urls_parse_to_bitbucket_forge(self, url: str, expected_slug: str) -> None:
        ref = RepoRef.from_url(url)
        assert ref.forge == "bitbucket"
        assert ref.slug() == expected_slug
        assert ref.owner == "workspace"
        assert ref.name == "repo"

    @pytest.mark.unit
    def test_bitbucket_https_with_userinfo_parses(self) -> None:
        ref = RepoRef.from_url("https://x-token-auth:secret@bitbucket.org/ws/repo.git")
        assert ref.forge == "bitbucket"
        assert ref.slug() == "ws/repo"

    @pytest.mark.unit
    @pytest.mark.parametrize(
        "url",
        [
            "git@gitlab.com:org/repo.git",
            "https://gitlab.com/org/repo.git",
            "https://example.com/o/r",
        ],
    )
    def test_unknown_host_still_raises(self, url: str) -> None:
        with pytest.raises(ValueError):
            RepoRef.from_url(url)

    @pytest.mark.unit
    @pytest.mark.parametrize(
        "url",
        [
            "https://bitbucket.org/workspace",
            "https://bitbucket.org/workspace/.git",
        ],
    )
    def test_incomplete_bitbucket_urls_raise(self, url: str) -> None:
        with pytest.raises(ValueError):
            RepoRef.from_url(url)

    @pytest.mark.unit
    @pytest.mark.parametrize(
        "url",
        [
            "https://github.com/only_owner",
            "ssh://git@github.com/only_owner",
        ],
    )
    def test_github_cannot_parse_message_is_byte_for_byte_unchanged(self, url: str) -> None:
        # The github-cannot-parse path must keep its original message verbatim so
        # downstream log/alert matching on "Cannot parse GitHub" keeps working
        # (plan contract; thread PRRT_kwDOSJAM6s6GQy8X).
        with pytest.raises(ValueError, match=r"^Cannot parse GitHub repo from URL: "):
            RepoRef.from_url(url)

    @pytest.mark.unit
    @pytest.mark.parametrize(
        "url",
        [
            "https://bitbucket.org/workspace",
            "git@gitlab.com:org/repo.git",
        ],
    )
    def test_non_github_parse_failures_avoid_github_label(self, url: str) -> None:
        # Recognized non-github hosts name their host; unknown hosts stay
        # forge-agnostic (thread PRRT_kwDOSJAM6s6GQcgc) — neither says "GitHub".
        with pytest.raises(ValueError, match=r"^Cannot parse (?!GitHub repo)"):
            RepoRef.from_url(url)


class TestRepoRefHostAwareBuilders:
    @pytest.mark.unit
    def test_github_builders_unchanged(self) -> None:
        ref = RepoRef(owner="o", name="r")  # default forge="github"
        assert ref.https_url() == "https://github.com/o/r.git"
        assert ref.ssh_url() == "git@github.com:o/r.git"

    @pytest.mark.unit
    def test_bitbucket_builders_emit_bitbucket_host(self) -> None:
        ref = RepoRef(owner="ws", name="repo", forge="bitbucket")
        assert ref.https_url() == "https://bitbucket.org/ws/repo.git"
        assert ref.ssh_url() == "git@bitbucket.org:ws/repo.git"
        assert ref.slug() == "ws/repo"

    @pytest.mark.unit
    @pytest.mark.parametrize(
        "reference_url, expected_url",
        [
            (
                "git@bitbucket.org:src/source.git",
                "git@bitbucket.org:ws/repo.git",
            ),
            (
                "ssh://git@bitbucket.org/src/source.git",
                "git@bitbucket.org:ws/repo.git",
            ),
            (
                # Explicit default SSH port must stay SSH (thread
                # PRRT_kwDOSJAM6s6IQkBd), not fall through to HTTPS.
                "ssh://git@bitbucket.org:22/src/source.git",
                "git@bitbucket.org:ws/repo.git",
            ),
            (
                "https://bitbucket.org/src/source.git",
                "https://bitbucket.org/ws/repo.git",
            ),
            (
                "https://x-token-auth:secret@bitbucket.org/src/source.git",
                "https://x-token-auth:secret@bitbucket.org/ws/repo.git",
            ),
            (
                "file:///tmp/source",
                "https://bitbucket.org/ws/repo.git",
            ),
        ],
    )
    def test_clone_url_like_bitbucket_transport(
        self, reference_url: str, expected_url: str
    ) -> None:
        ref = RepoRef(owner="ws", name="repo", forge="bitbucket")
        assert ref.clone_url_like(reference_url) == expected_url

    @pytest.mark.unit
    def test_clone_url_like_github_transport_unchanged(self) -> None:
        ref = RepoRef(owner="contributor", name="aira-web")
        assert (
            ref.clone_url_like("git@github.com:dimileeh/source.git")
            == "git@github.com:contributor/aira-web.git"
        )
        assert (
            ref.clone_url_like("https://github.com/dimileeh/source.git")
            == "https://github.com/contributor/aira-web.git"
        )
        # Explicit default SSH port stays SSH (thread PRRT_kwDOSJAM6s6IQkBd).
        assert (
            ref.clone_url_like("ssh://git@github.com:22/dimileeh/source.git")
            == "git@github.com:contributor/aira-web.git"
        )
