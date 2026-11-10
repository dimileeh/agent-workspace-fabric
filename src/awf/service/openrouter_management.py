"""OpenRouter key-management client used only by the API and worker processes.

The management key authenticates key provisioning; it is never forwarded to
agent stacks, hosted payloads, logs, argv or the database. Minted key values
are returned once, held in memory for one invocation and never persisted.
"""

from __future__ import annotations

import os

OPENROUTER_MANAGEMENT_KEY_ENV = "OPENROUTER_MANAGEMENT_KEY"


def scrub_openrouter_management_environ() -> None:
    """Drop the management key from ``os.environ`` once settings have resolved it.

    Docker Compose, git and agent subprocesses inherit the worker environment;
    removing the variable keeps them from interpolating or inheriting it.
    """
    os.environ.pop(OPENROUTER_MANAGEMENT_KEY_ENV, None)
