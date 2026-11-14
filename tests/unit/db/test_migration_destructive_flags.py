"""Every migration declares ``destructive``; a destructive ``upgrade()`` must say True (#1223).

The scan reads only the literal ``upgrade()`` body: ``downgrade()`` legitimately
drops what ``upgrade()`` created, and calls into helpers are not followed. The
guard is one-directional — a flagged upgrade requires ``destructive = True``, and
authors may still declare True by judgement.
"""

from __future__ import annotations

import ast
import re
import textwrap
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_VERSIONS = Path(__file__).resolve().parents[3] / "migrations" / "versions"
_MIGRATIONS = sorted(_VERSIONS.glob("*.py"))
_DROP_OPS = frozenset(
    {"drop_table", "drop_column", "drop_index", "drop_constraint", "rename_table"}
)
_RISKY_ALTER_KEYWORDS = frozenset({"type_", "new_column_name"})
_EXECUTE_ATTRS = frozenset({"execute", "exec_driver_sql"})
_DESTRUCTIVE_SQL = (
    re.compile(r"\bDROP\b", re.IGNORECASE),
    re.compile(r"\bALTER\b[^;]*?\bTYPE\b", re.IGNORECASE | re.DOTALL),
    re.compile(r"\bRENAME\b", re.IGNORECASE),
)
_INVALID = "invalid"


def _declared_destructive(tree: ast.Module) -> bool | str | None:
    """Return the module-level bool flag, ``None`` if absent, or ``"invalid"``."""
    values: list[ast.expr | None] = []
    for node in tree.body:
        if (
            isinstance(node, ast.Assign)
            and any(
                isinstance(target, ast.Name) and target.id == "destructive"
                for target in node.targets
            )
            or (
                isinstance(node, ast.AnnAssign)
                and isinstance(node.target, ast.Name)
                and node.target.id == "destructive"
            )
        ):
            values.append(node.value)
    if not values:
        return None
    if len(values) > 1:
        return _INVALID
    value = values[0]
    if isinstance(value, ast.Constant) and isinstance(value.value, bool):
        return value.value
    return _INVALID


def _upgrade(tree: ast.Module) -> ast.FunctionDef | None:
    return next(
        (
            node
            for node in tree.body
            if isinstance(node, ast.FunctionDef) and node.name == "upgrade"
        ),
        None,
    )


def _literal_sql(node: ast.expr) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if (
        isinstance(node, ast.Call)
        and (
            (isinstance(node.func, ast.Name) and node.func.id == "text")
            or (isinstance(node.func, ast.Attribute) and node.func.attr == "text")
        )
        and len(node.args) == 1
    ):
        return _literal_sql(node.args[0])
    return None


def _upgrade_destructive_reasons(tree: ast.Module) -> list[str]:
    upgrade = _upgrade(tree)
    assert upgrade is not None, "migration has no upgrade()"
    reasons: list[str] = []
    for node in ast.walk(upgrade):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
            continue
        attr = node.func.attr
        if attr in _DROP_OPS:
            reasons.append(attr)
        elif attr == "alter_column":
            keywords = {keyword.arg for keyword in node.keywords} & _RISKY_ALTER_KEYWORDS
            reasons.extend(f"alter_column({keyword}=)" for keyword in sorted(keywords))
        elif attr in _EXECUTE_ATTRS:
            sql = _literal_sql(node.args[0]) if node.args else None
            if sql is None:
                reasons.append(f"{attr}(<unreadable SQL>)")
            elif any(pattern.search(sql) for pattern in _DESTRUCTIVE_SQL):
                reasons.append(f"{attr}({sql.strip()[:60]!r})")
    return reasons


def _violations(source: str) -> list[str]:
    tree = ast.parse(textwrap.dedent(source))
    declared = _declared_destructive(tree)
    if declared is None or declared == _INVALID:
        return ["destructive flag missing or invalid"]
    reasons = _upgrade_destructive_reasons(tree)
    return reasons if reasons and declared is not True else []


@pytest.mark.parametrize("path", _MIGRATIONS, ids=lambda path: path.stem)
def test_every_migration_declares_destructive_flag(path: Path) -> None:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    assert _upgrade(tree) is not None
    assert isinstance(_declared_destructive(tree), bool)


@pytest.mark.parametrize("path", _MIGRATIONS, ids=lambda path: path.stem)
def test_destructive_upgrade_requires_flag(path: Path) -> None:
    assert _violations(path.read_text(encoding="utf-8")) == []


def test_migrations_are_discovered() -> None:
    assert len(_MIGRATIONS) >= 56


_HEADER = """
import sqlalchemy as sa
from alembic import op

destructive = False
SQL_NAME = "DROP TABLE x"
"""
_FLAGGED_UPGRADES = {
    "drop_table": 'op.drop_table("t")',
    "drop_column": 'op.drop_column("t", "c")',
    "drop_index": 'op.drop_index("ix", table_name="t")',
    "drop_constraint": 'op.drop_constraint("uq", "t", type_="unique")',
    "batch_drop_column": (
        'with op.batch_alter_table("t") as batch_op:\n        batch_op.drop_column("c")'
    ),
    "alter_type": 'op.alter_column("t", "c", type_=sa.BigInteger())',
    "alter_rename": 'op.alter_column("t", "c", new_column_name="d")',
    "rename_table": 'op.rename_table("a", "b")',
    "execute_drop": 'op.execute("DROP TABLE x")',
    "execute_alter_type": 'op.execute(sa.text("ALTER TABLE t ALTER COLUMN c TYPE bigint"))',
    "execute_rename": 'op.execute("ALTER TABLE a RENAME TO b")',
    "execute_name": "op.execute(SQL_NAME)",
    "execute_fstring": 'op.execute(f"CREATE INDEX {SQL_NAME}")',
    "bind_execute_drop": 'op.get_bind().execute(sa.text("DROP INDEX i"))',
    "exec_driver_sql_no_args": "op.get_bind().exec_driver_sql()",
}


def _migration(upgrade: str, downgrade: str = "pass", header: str = _HEADER) -> str:
    return (
        f"{header}\n\ndef upgrade() -> None:\n    {upgrade}\n\n\n"
        f"def downgrade() -> None:\n    {downgrade}\n"
    )


@pytest.mark.parametrize("case", sorted(_FLAGGED_UPGRADES))
def test_destructive_upgrade_declared_false_is_rejected(case: str) -> None:
    source = _migration(_FLAGGED_UPGRADES[case])
    assert _violations(source) != []
    assert _violations(source.replace("destructive = False", "destructive = True")) == []


@pytest.mark.parametrize(
    ("upgrade", "downgrade"),
    [
        ('op.add_column("t", sa.Column("c", sa.Integer()))', 'op.drop_column("t", "c")'),
        ('op.create_index("ix", "t", ["c"])', 'op.execute("DROP INDEX ix")'),
        ('op.create_table("t")', 'op.drop_table("t")'),
        ('op.alter_column("t", "c", nullable=False)', "pass"),
        ('op.execute("CREATE INDEX ix ON t (c)")', "pass"),
        ('op.execute(text("UPDATE t SET c = 1"))', "pass"),
    ],
)
def test_non_destructive_upgrade_is_accepted(upgrade: str, downgrade: str) -> None:
    assert _violations(_migration(upgrade, downgrade)) == []


@pytest.mark.parametrize(
    "header",
    [
        "from alembic import op\n",
        'from alembic import op\ndestructive = "yes"\n',
        "from alembic import op\ndestructive = False\ndestructive: bool = True\n",
    ],
)
def test_missing_or_invalid_declaration_is_rejected(header: str) -> None:
    assert _violations(_migration("pass", header=header)) == ["destructive flag missing or invalid"]


def test_annotated_declaration_is_accepted() -> None:
    tree = ast.parse("destructive: bool = True\n")
    assert _declared_destructive(tree) is True
