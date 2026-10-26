"""PostgreSQL admission mutex, including raw SQL claim and reservation writes.

These guards are installed by migration, never by a runtime process. They are
inert without the explicit lifecycle row. Application roles must not own DDL.
"""

import json

from sqlalchemy import event, text
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import Session, SessionTransaction

from awf.hosted_node.schemas import Identity

GUARDED_TABLES = (
    "workspaces",
    "tasks",
    "task_attempts",
    "operations",
    "validation_runs",
    "resource_reservations",
    "service_gc_requests",
    "callback_deliveries",
    "callback_subscriptions",
    "merge_candidates",
    "workspace_events",
    "workspace_secret_leases",
)
_IDENTITY_KEY = "awf_node_identity"


@event.listens_for(Session, "after_begin")
def _pin_identity(
    session: Session, _transaction: SessionTransaction, connection: Connection
) -> None:
    identity = session.info.get(_IDENTITY_KEY)
    if identity is not None:
        connection.execute(
            text("SELECT set_config('awf.node_identity', :value, true)"),
            {
                "value": identity,
            },
        )


def bind_factory(factory: async_sessionmaker[AsyncSession], identity: Identity) -> None:
    """Pin a trusted boot identity, never infer authority from the latest DB row."""
    factory.configure(
        info={
            **factory.kw.get("info", {}),
            _IDENTITY_KEY: json.dumps(identity.model_dump()),
        }
    )


def install_barrier(connection: Connection) -> None:
    connection.execute(
        text("""
        CREATE OR REPLACE FUNCTION awf_node_admission_guard() RETURNS trigger
        LANGUAGE plpgsql AS $$
        DECLARE gate hosted_node_lifecycle%ROWTYPE;
        BEGIN
            SELECT * INTO gate FROM hosted_node_lifecycle WHERE singleton = 1 FOR UPDATE;
            IF FOUND THEN
                IF NULLIF(current_setting('awf.node_identity', true), '')::jsonb
                   IS DISTINCT FROM gate.identity THEN
                    RAISE EXCEPTION 'NODE_IDENTITY_MISMATCH' USING ERRCODE = '55000';
                END IF;
                IF gate.phase <> 'ready' THEN
                    RAISE EXCEPTION 'NODE_ADMISSION_CLOSED' USING ERRCODE = '55000';
                END IF;
                UPDATE hosted_node_lifecycle SET idle_since = NULL WHERE singleton = 1;
            ELSIF NULLIF(current_setting('awf.node_identity', true), '') IS NOT NULL THEN
                RAISE EXCEPTION 'NODE_GATE_MISSING' USING ERRCODE = '55000';
            END IF;
            IF TG_OP = 'DELETE' THEN RETURN OLD; END IF;
            RETURN NEW;
        END $$
    """)
    )
    for table in GUARDED_TABLES:
        connection.execute(
            text(f"""
            CREATE TRIGGER awf_node_admission BEFORE INSERT OR UPDATE OR DELETE ON {table}
            FOR EACH ROW EXECUTE FUNCTION awf_node_admission_guard()
        """)
        )


async def verify_barrier(session: AsyncSession) -> None:
    result = await session.execute(
        text("""
        SELECT c.relname FROM pg_trigger t JOIN pg_class c ON c.oid = t.tgrelid
        WHERE t.tgname = 'awf_node_admission' AND t.tgenabled IN ('O', 'A')
          AND c.relnamespace = current_schema()::regnamespace
    """)
    )
    if set(result.scalars()) != set(GUARDED_TABLES):
        from awf.hosted_node.identity import MaterialDeniedError

        raise MaterialDeniedError("NODE_BARRIER_UNAVAILABLE", 503)
