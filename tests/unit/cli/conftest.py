"""Pytest fixtures shared across the CLI test suites.

Re-export the ``awf setup`` harness fixtures so both
:mod:`test_setup_commands` and :mod:`test_setup_commands_client` can request
them by name without importing the fixture functions (which would read as an
unused import to linters).
"""

from __future__ import annotations

from tests.unit.cli._setup_commands_shared import client_harness, harness

__all__ = ["client_harness", "harness"]
