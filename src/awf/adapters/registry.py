"""Module that eagerly imports every adapter so the registry is populated.

Importing this module has the side effect of registering all adapter classes
via ``@register_adapter``. Callers that use ``get_adapter(...)`` must import
this module once at application startup (the FastAPI app factory does so).
"""

from __future__ import annotations

# These imports have the side effect of registering each class in
# awf.adapters.base._REGISTRY. Don't remove them — they're load-bearing.
from awf.adapters import claude_code, codex, cursor, gemini, grok, opencode  # noqa: F401

__all__: list[str] = []
