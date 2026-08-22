"""Shared request contract for provider-specific Cursor Auto routing."""

from __future__ import annotations

from typing import Self

from pydantic import BaseModel, Field, model_validator

from awf.db.enums import AgentRuntime, CursorAutoMode


class CursorAutoModeResponseMixin(BaseModel):
    """Project the persisted product-facing Cursor Auto mode."""

    cursor_auto_mode: CursorAutoMode | None = None


class CursorAutoModeSelectionMixin(BaseModel):
    """Add and validate Cursor Router mode on API request models."""

    cursor_auto_mode: CursorAutoMode | None = Field(
        default=None,
        description=(
            "Cursor Router optimization mode for Cursor Auto. Requires agent='cursor', "
            "no generic effort, and no fixed model override."
        ),
    )

    @model_validator(mode="after")
    def _validate_cursor_auto_mode(self) -> Self:
        mode = self.cursor_auto_mode
        if mode is None:
            return self
        if getattr(self, "agent", None) is not AgentRuntime.cursor:
            raise ValueError("cursor_auto_mode requires agent='cursor'.")
        if getattr(self, "effort", None) is not None:
            raise ValueError(
                "cursor_auto_mode cannot be combined with generic effort; Cursor Auto "
                "uses Cost, Balance, or Intelligence routing modes instead."
            )
        model = getattr(self, "model", None)
        if model is not None and model != "auto":
            raise ValueError(
                "cursor_auto_mode cannot be combined with a fixed model override; "
                "omit model or set model='auto'."
            )
        execution = getattr(self, "execution", None)
        if getattr(execution, "mode", None) == "hosted":
            raise ValueError(
                "cursor_auto_mode is not yet supported for hosted PR adoption; use local "
                "execution until the AWF Cloud contract carries Cursor Router mode."
            )
        return self
