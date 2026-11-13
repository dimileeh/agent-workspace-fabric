"""The packaged checkpoint schema contract: one machine-readable source of truth.

``checkpoint_contract.json`` pins the Alembic chain, the head the checkpoint cut
requires, the cut tables and the expected trigger/sequence catalog. Runtime
constants load from it; parity tests tie it to migrations and a migrated schema.
"""

from importlib.resources import files
from typing import Annotated, Literal

from pydantic import Field, model_validator

from awf.hosted_node.schemas import StrictModel

Revision = Annotated[str, Field(strict=True, pattern=r"^[0-9a-f]{12}$")]
Name = Annotated[str, Field(strict=True, min_length=1, max_length=63)]


def _sorted_unique(values: tuple[object, ...]) -> bool:
    return list(values) == sorted(set(values))  # type: ignore[type-var]


class CheckpointContract(StrictModel):
    schema_version: Literal[1]
    schema_revision: Revision
    tables: tuple[Name, ...]
    guarded_tables: tuple[Name, ...]
    triggers: tuple[tuple[Name, Name, Name], ...]
    sequences: tuple[Name, ...]
    revisions: tuple[Revision, ...]
    destructive_revisions: tuple[Revision, ...]

    @model_validator(mode="after")
    def consistent(self) -> "CheckpointContract":
        if not all(
            _sorted_unique(values)
            for values in (self.tables, self.guarded_tables, self.triggers, self.sequences)
        ):
            raise ValueError("CHECKPOINT_CONTRACT_NOT_SORTED_UNIQUE")
        if len(set(self.revisions)) != len(self.revisions) or self.revisions[-1:] != (
            self.schema_revision,
        ):
            raise ValueError("CHECKPOINT_CONTRACT_REVISION_CHAIN")
        position = {revision: index for index, revision in enumerate(self.revisions)}
        indexes = [position.get(revision, -1) for revision in self.destructive_revisions]
        if -1 in indexes or indexes != sorted(set(indexes)):
            raise ValueError("CHECKPOINT_CONTRACT_DESTRUCTIVE_REVISIONS")
        if not set(self.guarded_tables) <= set(self.tables):
            raise ValueError("CHECKPOINT_CONTRACT_GUARDED_TABLES")
        return self


def parse_contract(raw: str | bytes) -> CheckpointContract:
    """Validate the contract JSON; malformed text or shape raises ``ValueError``."""
    return CheckpointContract.model_validate_json(raw)


CONTRACT = parse_contract(
    files("awf.hosted_node").joinpath("checkpoint_contract.json").read_bytes()
)
