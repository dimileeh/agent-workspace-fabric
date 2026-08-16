"""Exceptions emitted by Compose lifecycle operations."""

from __future__ import annotations


class ComposeOperationError(Exception):
    """Raised when a ``docker compose`` command exits non-zero."""

    def __init__(
        self,
        *,
        operation: str,
        returncode: int,
        stdout: str,
        stderr: str,
        reason_code: str = "COMPOSE_COMMAND_FAILED",
    ) -> None:
        """Capture the failed compose operation and its diagnostic streams."""
        self.operation = operation
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        self.reason_code = reason_code
        super().__init__(
            f"docker compose {operation} failed "
            f"(exit={returncode}, reason={reason_code}): "
            f"{stderr.strip() or stdout.strip() or '<no output>'}"
        )
