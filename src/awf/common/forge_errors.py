"""Forge-neutral client-error base shared by the GitHub and BitBucket clients.

``GitHubClientError`` and ``BitBucketClientError`` historically extended
``Exception`` directly with no common ancestor, so PR-monitor catch sites that
listed only ``except GitHubClientError`` silently let BitBucket faults (429/auth/
409) escape uncaught and could terminate the monitor (issue #444). ``ForgeClientError``
is that common ancestor: a single ``except ForgeClientError`` arm catches both.

The base lives in its own tiny module — not in ``github_client`` or
``bitbucket_client`` — to keep it forge-neutral and avoid a github↔bitbucket import
cycle (``bitbucket_client`` already imports ``RepoRef`` from ``github_client``).

The base only *normalizes* fields the catch sites read; it does not rename the
subclass-specific fields. ``GitHubClientError`` keeps ``returncode``/``stderr``;
``BitBucketClientError`` keeps ``status``/``reason_code``/``body``. The normalized
accessors below expose a uniform contract over both:

* ``reason_code`` — a stable, end-to-end reason code. BitBucket sets one natively;
  GitHub (which shells ``gh`` and has no HTTP status) defaults to ``GITHUB_API_ERROR``.
* ``redacted_detail()`` — the already-redacted human detail (GitHub stderr / BitBucket
  body).
* ``merge_method_stderr()`` — the GitHub-stderr-equivalent the merge-method-mismatch
  parser consumes. GitHub returns its stderr; BitBucket returns ``""`` (it carries no
  per-method rejection signal), so the parser can read the base without an isinstance.
* ``http_status`` — the HTTP status (BitBucket) or ``None`` (GitHub shells ``gh``).
  Used only by transient classification, never by the GitHub-specific parse paths.

Transient classification is intentionally NOT a property here: it depends on the
runtime-layer marker constants (``runtime.pr_monitor_runner.constants``) and the
``common`` layer must not import from ``runtime``. The PR monitor classifies via
``runtime.pr_monitor_runner.helpers`` instead.
"""

from __future__ import annotations


class ForgeClientError(Exception):
    """Common base for forge (GitHub/BitBucket) client errors.

    Concrete subclasses set ``operation`` and ``reason_code`` in their ``__init__``
    and override the normalized accessors below. The base provides safe defaults so
    a catch site can read the uniform contract over either subclass.
    """

    operation: str
    reason_code: str

    def redacted_detail(self) -> str:
        """Return the already-redacted human-facing failure detail.

        Subclasses override to return their redacted stderr (GitHub) or body
        (BitBucket).
        """
        return str(self)

    def merge_method_stderr(self) -> str:
        """Return the GitHub-stderr-equivalent for the merge-method-mismatch parser.

        Only GitHub carries a per-method rejection signal in stderr; every other
        forge returns ``""`` so the parser can consume the base without an
        ``isinstance`` check.
        """
        return ""

    @property
    def http_status(self) -> int | None:
        """Return the HTTP status when the forge speaks HTTP, else ``None``.

        GitHub shells ``gh`` and has no HTTP status; BitBucket overrides this with
        its ``status`` (also ``None`` for transport-level faults).
        """
        return None
