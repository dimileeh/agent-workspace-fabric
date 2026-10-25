"""Optional trusted consumer boundary; ordinary local consumers are unchanged."""

from collections.abc import Callable, Coroutine
from contextlib import AbstractAsyncContextManager
from functools import wraps
from typing import Any, cast

WorkspaceScope = Callable[[str], AbstractAsyncContextManager[None]]


def workspace_consumer[F: Callable[..., Coroutine[Any, Any, Any]]](operation: F) -> F:
    @wraps(operation)
    async def guarded(self: Any, workspace_id: str, *args: Any, **kwargs: Any) -> Any:
        scope: WorkspaceScope | None = getattr(self, "_workspace_scope", None)
        if scope is None:
            return await operation(self, workspace_id, *args, **kwargs)
        async with scope(workspace_id):
            return await operation(self, workspace_id, *args, **kwargs)

    return cast(F, guarded)
