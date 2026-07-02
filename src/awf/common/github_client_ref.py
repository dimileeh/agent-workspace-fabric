"""Repository reference parsing + URL builders for the GitHub client.

``RepoRef`` is the stateless owner/name/forge value object parsed out of clone
URLs and bare slugs. It carries no dependency on ``GitHubClient`` itself, so it
lives here to keep ``github_client.py`` under the first-party file-size guardrail
while remaining importable from ``awf.common.github_client`` (compatibility
re-export) for the many call sites that reference it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlsplit

from awf.db.enums import ForgeKind

# Forge → canonical hostname. Used by ``RepoRef`` detection + URL builders so a
# single mapping keeps parsing and emission in lockstep (issue #345 Phase 1).
_FORGE_HOSTS: dict[ForgeKind, str] = {
    "github": "github.com",
    "bitbucket": "bitbucket.org",
}
_HOST_FORGES: dict[str, ForgeKind] = {host: forge for forge, host in _FORGE_HOSTS.items()}


def _strip_bare_slug_git_suffix(name: str) -> str:
    """Strip a trailing ``.git`` from a bare-slug repo name.

    Replicates the original lazy ``([^/\\s]+?)(?:\\.git)?`` behavior exactly: the
    suffix is removed only when something precedes it (``len > 4``), so
    ``owner/.git`` keeps the literal ``.git`` name while ``owner/repo.git``
    becomes ``repo`` and ``owner/repo.git.git`` becomes ``repo.git``.
    """
    if name.endswith(".git") and len(name) > 4:
        return name[:-4]
    return name


@dataclass(frozen=True)
class RepoRef:
    """Owner + repo name parsed out of URLs like
    ``git@github.com:org/repo.git`` or ``https://github.com/org/repo``.

    ``forge`` records which code-forge the ref was detected on (``github`` by
    default; ``bitbucket`` for ``bitbucket.org`` URLs). The URL builders are
    host-aware off ``forge`` so a Bitbucket ref emits ``bitbucket.org`` URLs.
    GitHub behavior is unchanged when ``forge == "github"`` (the common path)."""

    owner: str
    name: str
    forge: ForgeKind = "github"

    @classmethod
    def from_url(cls, repo_url: str) -> RepoRef:
        """Parse a repository URL/slug into a `RepoRef`, detecting the forge by host.

        Recognized hosts: ``github.com`` (forge ``github``) and ``bitbucket.org``
        (forge ``bitbucket``). A bare ``owner/repo`` slug (no host, no scheme)
        defaults to GitHub. Any other host preserves the existing ``ValueError``.
        """
        value = repo_url.strip()
        # Bare ``owner/repo`` slug (no host, no scheme) defaults to GitHub.
        # Possessive groups (``++``) make the owner/name split unambiguous — ``/``
        # is excluded from the class, so a token never needs to give a character
        # back — eliminating the ReDoS backtracking the lazy ``([^/\s]+?)`` +
        # ``(?:\.git)?`` overlap allowed (CodeQL py/redos). The trailing ``.git``
        # strip moves into code to preserve the original lazy behavior exactly.
        slug_match = re.fullmatch(r"([^/\s]++)/([^/\s]++)/?", value)
        if (
            slug_match
            and "github.com" not in value
            and "bitbucket.org" not in value
            and ":" not in value
        ):
            return cls(
                owner=slug_match.group(1),
                name=_strip_bare_slug_git_suffix(slug_match.group(2)),
                forge="github",
            )

        # SSH scp-like form: ``git@<host>:owner/repo(.git)?``.
        # Possessive groups (``++``) make the owner/name split unambiguous — ``/``
        # is excluded from the class, so a token never needs to give a character
        # back — eliminating the same lazy ``([^/]+?)`` + ``(?:\.git)?`` overlap
        # (CodeQL py/redos) that was hardened on the bare-slug path above. The
        # trailing ``.git`` strip moves into ``_strip_bare_slug_git_suffix`` so the
        # original lazy behavior (only a non-empty suffix is stripped) is preserved.
        for host, forge in _HOST_FORGES.items():
            ssh_match = re.fullmatch(rf"git@{re.escape(host)}:([^/]++)/([^/]++)/?", value)
            if ssh_match:
                return cls(
                    owner=ssh_match.group(1),
                    name=_strip_bare_slug_git_suffix(ssh_match.group(2)),
                    forge=forge,
                )

        # ``urlsplit``/``.hostname`` raise a bespoke ``ValueError`` (e.g. "Invalid
        # IPv6 URL") on malformed authorities like ``https://[bad``. Normalize that
        # to this method's standard parse-failure message so callers see one
        # consistent error and no urllib internals leak through.
        try:
            parsed = urlsplit(value)
            parsed_host = parsed.hostname.lower() if parsed.hostname is not None else None
        except ValueError as exc:
            raise ValueError(f"Cannot parse repo from URL: {repo_url!r}") from exc
        url_forge = _HOST_FORGES.get(parsed_host) if parsed_host is not None else None
        is_http_url = parsed.scheme in {"http", "https"}
        is_ssh_url = parsed.scheme == "ssh" and (
            parsed.username is None or parsed.username.lower() == "git"
        )
        if url_forge is not None and (is_http_url or is_ssh_url):
            parts = [part for part in parsed.path.strip("/").split("/") if part]
            if len(parts) >= 2 and parts[0] and parts[1]:
                name = parts[1][:-4] if parts[1].endswith(".git") else parts[1]
                if name:
                    return cls(owner=parts[0], name=name, forge=url_forge)
            # Preserve the original byte-for-byte message on the github-cannot-parse
            # path (plan contract); other forges name their host for a clear error.
            label = "GitHub" if url_forge == "github" else _FORGE_HOSTS[url_forge]
            raise ValueError(f"Cannot parse {label} repo from URL: {repo_url!r}")

        raise ValueError(f"Cannot parse repo from URL: {repo_url!r}")

    def _forge_host(self) -> str:
        """Return the canonical hostname for this ref's forge."""
        return _FORGE_HOSTS[self.forge]

    def slug(self) -> str:
        """Return the repository slug in `owner/name` format."""
        return f"{self.owner}/{self.name}"

    def https_url(self) -> str:
        """Return HTTPS clone URL for the repository."""
        return f"https://{self._forge_host()}/{self.owner}/{self.name}.git"

    def ssh_url(self) -> str:
        """Return SSH clone URL for the repository."""
        return f"git@{self._forge_host()}:{self.owner}/{self.name}.git"

    def clone_url_like(self, repo_url: str) -> str:
        """Return a clone URL matching the requested transport style."""
        host = self._forge_host()
        stripped = repo_url.strip()
        # ``urlsplit``/``.hostname`` raise ``ValueError`` (e.g. "Invalid IPv6 URL")
        # on malformed authorities like ``https://[bad``. Unlike ``from_url``, this
        # method returns a URL rather than parsing one, so an unhandled crash here
        # would be unexpected: treat an unparseable URL as a non-match and fall back
        # to the canonical HTTPS clone URL (same as other unrecognized inputs).
        try:
            parsed = urlsplit(stripped)
            parsed_host = parsed.hostname.lower() if parsed.hostname is not None else None
        except ValueError:
            return self.https_url()
        # Match SSH by scheme (not a no-port prefix) so explicit-port forms such
        # as ssh://git@github.com:22/owner/repo.git are preserved as SSH instead
        # of silently falling through to HTTPS (thread PRRT_kwDOSJAM6s6IQkBd).
        is_ssh_url = (
            parsed.scheme == "ssh"
            and parsed_host == host
            and (parsed.username is None or parsed.username.lower() == "git")
        )
        if stripped.startswith(f"git@{host}:") or is_ssh_url:
            return self.ssh_url()

        if parsed.scheme in {"http", "https"} and parsed_host == host:
            userinfo, sep, _host = parsed.netloc.rpartition("@")
            if sep and userinfo:
                return f"https://{userinfo}@{host}/{self.owner}/{self.name}.git"
            return self.https_url()

        return self.https_url()
