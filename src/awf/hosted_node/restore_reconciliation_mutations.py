"""Fixed-table exact preimage plans, consumed by invoker triggers in one transaction."""

import json
from dataclasses import dataclass
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from awf.db.base import Base
from awf.hosted_node.identity import MaterialDeniedError
from awf.hosted_node.restore_reconciliation_models import RestoreReconciliationRow

TABLES = frozenset(
    {
        "workspaces",
        "operations",
        "workspace_events",
        "hosted_restore_row_holds",
        "hosted_node_lifecycle",
        "merge_candidates",
    }
)


@dataclass
class Mutations:
    entries: list[dict[str, Any]]
    updates: dict[str, list[dict[str, Any]]]
    inserts: dict[str, list[dict[str, Any]]]

    def __init__(self) -> None:
        self.entries, self.updates, self.inserts = [], {}, {}

    async def update(self, session: AsyncSession, family: str, edits: list[dict[str, Any]]) -> None:
        if not edits:
            return
        if family not in TABLES:
            raise MaterialDeniedError("RESTORE_MUTATION_UNSUPPORTED")
        keys = [column.name for column in Base.metadata.tables[family].primary_key]
        join = " AND ".join(f"to_jsonb(t)->>'{key}'=e.value->'key'->>'{key}'" for key in keys)
        rows = (
            await session.execute(
                text(
                    f"WITH planned AS (SELECT to_jsonb(t) AS before, to_jsonb(jsonb_populate_record(NULL::{family}, "
                    "to_jsonb(t) || (e.value->'changes'))) AS after "
                    f"FROM {family} t JOIN jsonb_array_elements(CAST(:edits AS jsonb)) e(value) ON {join} "
                    f"ORDER BY {','.join('t.' + key for key in keys)} FOR UPDATE OF t) "
                    "SELECT before,after,encode(sha256(convert_to(before::text,'UTF8')),'hex'),"
                    "encode(sha256(convert_to(after::text,'UTF8')),'hex') FROM planned"
                ),
                {"edits": json.dumps(edits)},
            )
        ).all()
        if len(rows) != len(edits):
            raise MaterialDeniedError("RESTORE_PREIMAGE_CHANGED")
        payloads = []
        for _before, after, old_hash, new_hash in rows:
            entry = self._entry(family, after, old_hash, new_hash)
            self.entries.append(entry)
            payloads.append(after)
        self.updates.setdefault(family, []).extend(payloads)

    async def insert(
        self, session: AsyncSession, family: str, payloads: list[dict[str, Any]]
    ) -> None:
        if not payloads:
            return
        if family not in TABLES:
            raise MaterialDeniedError("RESTORE_MUTATION_UNSUPPORTED")
        typed = (
            await session.execute(
                text(
                    "SELECT to_jsonb(r),encode(sha256(convert_to('{}','UTF8')),'hex'),"
                    "encode(sha256(convert_to(to_jsonb(r)::text,'UTF8')),'hex') "
                    f"FROM jsonb_populate_recordset(NULL::{family},CAST(:rows AS jsonb)) r"
                ),
                {"rows": json.dumps(payloads)},
            )
        ).all()
        for after, old_hash, new_hash in typed:
            self.entries.append(self._entry(family, after, old_hash, new_hash))
        self.inserts.setdefault(family, []).extend(row[0] for row in typed)

    @staticmethod
    def _entry(family: str, after: dict[str, Any], old_hash: str, new_hash: str) -> dict[str, Any]:
        # PostgreSQL's canonical JSONB rendering matches the trigger exactly,
        # including typed timestamps/decimals. No business payload is journaled.
        return {
            "family": family,
            "row_id": str(after.get("id", after.get("row_id", after.get("singleton")))),
            "before_sha256": old_hash,
            "after_sha256": new_hash,
            "used": False,
        }

    async def apply(self, session: AsyncSession, journal: RestoreReconciliationRow) -> None:
        journal.state = "applying"
        journal.transaction_id = await session.scalar(text("SELECT txid_current()"))
        journal.mutations = self.entries
        await session.flush()
        for family, payloads in self.inserts.items():
            await session.execute(
                text(
                    f"INSERT INTO {family} SELECT r.* FROM jsonb_populate_recordset(NULL::{family},CAST(:rows AS jsonb)) r"
                ),
                {"rows": json.dumps(payloads)},
            )
        for family, payloads in self.updates.items():
            table = Base.metadata.tables[family]
            keys = [column.name for column in table.primary_key]
            columns = [column.name for column in table.columns if column.name not in keys]
            await session.execute(
                text(
                    f"UPDATE {family} t SET ({','.join(columns)})=({','.join('r.' + col for col in columns)}) "
                    f"FROM jsonb_populate_recordset(NULL::{family},CAST(:rows AS jsonb)) r "
                    "WHERE " + " AND ".join(f"t.{key}=r.{key}" for key in keys)
                ),
                {"rows": json.dumps(payloads)},
            )
        await session.refresh(journal)
        if not all(entry["used"] for entry in journal.mutations):
            raise MaterialDeniedError("RESTORE_MUTATION_INCOMPLETE")
        journal.state, journal.transaction_id = "committed", None
