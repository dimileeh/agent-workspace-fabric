"""Typer CLI for AWF operators.

``awf`` wraps the REST API so humans (and shell scripts) can create/inspect
workspaces without hand-crafting curl invocations. The HTTP endpoint is
configurable via ``AWF_BASE_URL`` or ``--base-url``. ``AWF_CLI_BASE_URL`` is
still honored for compatibility, but is deprecated.
"""
