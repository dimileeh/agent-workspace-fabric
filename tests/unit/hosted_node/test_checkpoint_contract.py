"""The packaged checkpoint contract is the single source of the schema cut (#1223)."""

from __future__ import annotations

import json
from importlib.resources import files
from pathlib import Path
from typing import Any

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory

from awf.db.base import Base
from awf.hosted_node import checkpoint_cut
from awf.hosted_node.checkpoint_contract import CONTRACT, parse_contract
from awf.hosted_node.checkpoint_schemas import CHECKPOINT_SCHEMA_REVISION
from awf.hosted_node.lifecycle_barrier import GUARDED_TABLES

pytestmark = pytest.mark.unit

_REPO_ROOT = Path(__file__).resolve().parents[3]
_RAW = files("awf.hosted_node").joinpath("checkpoint_contract.json").read_text(encoding="utf-8")


def _script() -> ScriptDirectory:
    config = Config(str(_REPO_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(_REPO_ROOT / "migrations"))
    return ScriptDirectory.from_config(config)


def _chain(script: ScriptDirectory) -> list[str]:
    return [rev.revision for rev in reversed(list(script.walk_revisions("base", "heads")))]


def test_packaged_contract_is_canonical_json() -> None:
    assert json.dumps(json.loads(_RAW), indent=2, sort_keys=True) + "\n" == _RAW
    assert parse_contract(_RAW) == CONTRACT


def test_runtime_constants_load_from_contract() -> None:
    assert CONTRACT.schema_revision == CHECKPOINT_SCHEMA_REVISION
    assert CONTRACT.tables == checkpoint_cut.TABLES


def test_no_second_schema_revision_literal() -> None:
    offenders = [
        path.relative_to(_REPO_ROOT).as_posix()
        for path in (_REPO_ROOT / "src" / "awf").rglob("*.py")
        if CONTRACT.schema_revision in path.read_text(encoding="utf-8")
    ]
    assert offenders == []


def test_revisions_match_alembic_chain_and_head() -> None:
    script = _script()
    assert list(CONTRACT.revisions) == _chain(script)
    assert script.get_heads() == [CONTRACT.schema_revision] == [CONTRACT.revisions[-1]]


def test_destructive_revisions_match_migration_flags() -> None:
    script = _script()
    flagged = [rev for rev in _chain(script) if script.get_revision(rev).module.destructive]
    assert list(CONTRACT.destructive_revisions) == flagged


def test_guarded_tables_match_lifecycle_barrier() -> None:
    assert CONTRACT.guarded_tables == tuple(sorted(GUARDED_TABLES))


def test_tables_match_orm_metadata() -> None:
    assert CONTRACT.tables == tuple(sorted([*Base.metadata.tables, "alembic_version"]))


def _payload() -> dict[str, Any]:
    return json.loads(_RAW)


def _set(key: str, value: Any) -> dict[str, Any]:
    payload = _payload()
    payload[key] = value
    return payload


def _without(key: str) -> dict[str, Any]:
    payload = _payload()
    del payload[key]
    return payload


def _mutations() -> dict[str, dict[str, Any] | str]:
    payload = _payload()
    revisions = payload["revisions"]
    tables = payload["tables"]
    return {
        "invalid_json": _RAW[:-3],
        "not_an_object": "[]",
        "missing_key": _without("sequences"),
        "extra_key": _set("comment", "x"),
        "schema_version": _set("schema_version", 2),
        "revision_pattern": _set("schema_revision", "HEAD"),
        "unsorted_tables": _set("tables", list(reversed(tables))),
        "duplicate_sequence": _set("sequences", ["a_seq", "a_seq"]),
        "unsorted_triggers": _set("triggers", list(reversed(payload["triggers"]))),
        "trigger_arity": _set("triggers", [["workspaces", "awf_node_admission"]]),
        "head_mismatch": _set("schema_revision", revisions[0]),
        "empty_revisions": _set("revisions", []),
        "duplicate_revisions": _set("revisions", [revisions[0], *revisions]),
        "unknown_destructive": _set("destructive_revisions", ["0" * 12]),
        "destructive_order": _set(
            "destructive_revisions", list(reversed(payload["destructive_revisions"]))
        ),
        "guarded_not_in_tables": _set("guarded_tables", ["a_table_not_cut"]),
    }


@pytest.mark.parametrize("case", sorted(_mutations()))
def test_malformed_contract_raises(case: str) -> None:
    mutated = _mutations()[case]
    raw = mutated if isinstance(mutated, str) else json.dumps(mutated)
    with pytest.raises(ValueError):
        parse_contract(raw)
