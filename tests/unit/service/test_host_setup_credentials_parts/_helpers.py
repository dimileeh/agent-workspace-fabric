"""Shared fakes and fixtures for the host-setup credentials test parts.

These never touch a real OS keychain or the real ``keyring`` library: keyring
access is exercised through these injected fakes, and the plain-file backend
writes only under ``tmp_path``. The fixed fake tokens let every part assert a
secret never escapes into refs, error details, payloads, or captured output.
"""

from __future__ import annotations

from collections.abc import Callable

from awf.host_setup.credentials import HostCredentialCapabilities

# Fixed fake credential values; tests assert these never leak into outputs.
_FAKE_TOKEN = "sk-proj-" + ("a" * 48)
_FAKE_GH_TOKEN = "ghp_" + ("b" * 36)

_HEADLESS_LINUX = HostCredentialCapabilities(os_name="Linux", is_headless=True)
_DESKTOP_LINUX = HostCredentialCapabilities(os_name="Linux", is_headless=False)
_MACOS = HostCredentialCapabilities(os_name="Darwin", is_headless=False)
_WINDOWS = HostCredentialCapabilities(os_name="Windows", is_headless=False)


class _FakeKeyringError(Exception):
    """Stand-in for ``keyring.errors.KeyringError`` used by fakes."""


class _FakeKeyringErrors:
    """Namespace mirroring the real ``keyring.errors`` module surface."""

    KeyringError = _FakeKeyringError


class _UsableBackend:
    """Keyring backend whose module name does not denote a no-op backend."""


class _FailBackend:
    """Keyring backend that mimics ``keyring.backends.fail.Keyring``."""


# Mimic the import location the real no-op backend lives in.
_FailBackend.__module__ = "keyring.backends.fail"


class _ChainerBackend:
    """Keyring backend that mimics ``keyring.backends.chainer.ChainerBackend``.

    On headless Linux ``keyring`` can resolve to a chainer with no usable child
    backend: its module leaf is ``chainer`` (not ``fail``/``null``), so it passes
    the no-op availability probe, yet a write silently persists nothing.
    """


# Mimic the import location of the real chainer backend.
_ChainerBackend.__module__ = "keyring.backends.chainer"


class FakeKeyringModule:
    """Injectable fake keyring module that records secrets in memory only."""

    def __init__(
        self,
        *,
        backend: object | None = None,
        raise_on_get: bool = False,
        raise_on_set: bool = False,
        get_error: BaseException | None = None,
        set_error: BaseException | None = None,
        drop_writes: bool = False,
        read_back_error: BaseException | None = None,
    ) -> None:
        """Configure a fake keyring backend without touching any real keychain."""
        self._backend: object = _UsableBackend() if backend is None else backend
        self._raise_on_get = raise_on_get
        self._raise_on_set = raise_on_set
        self._get_error = get_error
        self._set_error = set_error
        # ``drop_writes`` models a backend (e.g. a ChainerBackend with no usable
        # child) whose ``set_password`` returns without raising yet persists
        # nothing, so a later ``get_password`` read-back finds the slot empty.
        self._drop_writes = drop_writes
        self._read_back_error = read_back_error
        self.errors = _FakeKeyringErrors
        self.set_calls: list[tuple[str, str, str]] = []
        self.delete_calls: list[tuple[str, str]] = []
        self._stored: dict[tuple[str, str], str] = {}

    def get_keyring(self) -> object:
        """Return the configured backend or raise like a missing keychain."""
        if self._get_error is not None:
            raise self._get_error
        if self._raise_on_get:
            raise _FakeKeyringError("no usable keyring backend")
        return self._backend

    def set_password(self, service: str, username: str, password: str) -> None:
        """Record a stored secret or raise like a locked/broken keychain."""
        if self._set_error is not None:
            raise self._set_error
        if self._raise_on_set:
            raise _FakeKeyringError("keychain locked")
        self.set_calls.append((service, username, password))
        if not self._drop_writes:
            self._stored[(service, username)] = password

    def get_password(self, service: str, username: str) -> str | None:
        """Return the stored secret, or raise like a broken keychain read."""
        if self._read_back_error is not None:
            raise self._read_back_error
        return self._stored.get((service, username))

    def delete_password(self, service: str, username: str) -> None:
        """Remove a stored secret, raising like keyring on a missing entry.

        The real ``keyring.delete_password`` raises ``PasswordDeleteError`` when
        the slot is empty, so model that here to exercise the best-effort
        cleanup's exception suppression on the no-orphan (write-dropped) path.
        """
        key = (service, username)
        self.delete_calls.append(key)
        if key not in self._stored:
            raise _FakeKeyringError("no such password")
        del self._stored[key]


def _secret(value: str | None) -> Callable[[], str | None]:
    """Return a lazy secret source callable yielding ``value``."""
    return lambda: value
