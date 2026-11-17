"""Optional trusted consumer boundary; ordinary local consumers are unchanged."""

from collections.abc import Callable, Coroutine, Iterator
from contextlib import AbstractAsyncContextManager, contextmanager
from contextvars import ContextVar
from functools import wraps
from typing import Any, cast

WorkspaceScope = Callable[[str], AbstractAsyncContextManager[None]]

# When true, hosted ``_scope`` raises ``WorkspaceAuthorityDeferredError`` on a
# grace-eligible entry failure instead of waiting inside the scope. Only the
# worker poll-inline preserved-recovery path sets this — blocking the poll on a
# full grace wait would stall every other workspace.
skip_authority_grace_wait: ContextVar[bool] = ContextVar(
    "awf_skip_authority_grace_wait",
    default=False,
)


@contextmanager
def without_authority_grace_wait() -> Iterator[None]:
    """Opt out of hosted scope-entry grace waits for the current context."""
    token = skip_authority_grace_wait.set(True)
    try:
        yield
    finally:
        skip_authority_grace_wait.reset(token)


def workspace_consumer[F: Callable[..., Coroutine[Any, Any, Any]]](operation: F) -> F:
    @wraps(operation)
    async def guarded(self: Any, workspace_id: str, *args: Any, **kwargs: Any) -> Any:
        scope: WorkspaceScope | None = getattr(self, "_workspace_scope", None)
        if scope is None:
            return await operation(self, workspace_id, *args, **kwargs)
        async with scope(workspace_id):
            return await operation(self, workspace_id, *args, **kwargs)

    return cast(F, guarded)
