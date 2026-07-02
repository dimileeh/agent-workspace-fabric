"""Stateless URL/path builders for the Bitbucket client.

Split out of ``bitbucket_client`` to keep that module under the first-party
line-count guardrail. These helpers derive Bitbucket REST paths and canonical
web URLs from a :class:`RepoRef` (and the raw PR payload) without touching any
client state, so they live cleanly on a mixin the client inherits.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

from awf.common.bitbucket_client_parsing import _as_dict, _clean_optional_str
from awf.common.github_client import RepoRef


class _BitbucketUrlsMixin:
    """Pure REST-path and web-URL builders mixed into ``BitbucketClient``."""

    @staticmethod
    def _pr_head_sha(pr: dict[str, Any]) -> str | None:
        return _clean_optional_str(_as_dict(_as_dict(pr.get("source")).get("commit")).get("hash"))

    def _repo_path(self, repo: RepoRef) -> str:
        return f"/2.0/repositories/{quote(repo.owner, safe='')}/{quote(repo.name, safe='')}"

    def _pr_collection_path(self, repo: RepoRef) -> str:
        return f"{self._repo_path(repo)}/pullrequests"

    def _pr_path(self, repo: RepoRef, pr_number: int) -> str:
        return f"{self._pr_collection_path(repo)}/{pr_number}"

    def _issues_page_url(self, repo: RepoRef) -> str:
        return f"https://bitbucket.org/{repo.owner}/{repo.name}/issues"

    def _issue_url_from_id(self, data: Any, repo: RepoRef) -> str | None:
        """Build the canonical issue URL from a created issue's numeric ``id``.

        Bitbucket's create-issue response carries an integer ``id`` even when it
        omits ``links.html.href``; deriving ``.../issues/{id}`` from it keeps the
        tracking URL pointing at the specific filed issue instead of the generic
        list. Returns ``None`` when ``data`` is not a dict or lacks a usable id.
        """
        issue_id = data.get("id") if isinstance(data, dict) else None
        if isinstance(issue_id, int):
            return f"{self._issues_page_url(repo)}/{issue_id}"
        return None

    def _pr_page_url(self, repo: RepoRef, pr_number: int) -> str:
        return f"https://bitbucket.org/{repo.owner}/{repo.name}/pull-requests/{pr_number}"
