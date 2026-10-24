"""Observe the connected database and deny migration/bypass-capable logins."""

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from awf.hosted_node.host_config import HostSettings
from awf.hosted_node.lifecycle_barrier import GUARDED_TABLES, verify_barrier


async def validate_database(session: AsyncSession, config: HostSettings) -> None:
    row = (
        (
            await session.execute(
                text("""
        SELECT current_database() AS database, current_schema() AS schema,
               current_user AS role, session_user AS login,
               current_schemas(false) AS schemas,
               current_setting('session_replication_role') AS replication,
               r.rolsuper OR r.rolbypassrls OR r.rolcreatedb OR r.rolcreaterole
                 OR r.rolreplication AS elevated,
               EXISTS (SELECT FROM pg_auth_members WHERE member = r.oid) AS membership,
               has_database_privilege(current_database(), 'CREATE') OR
                 has_database_privilege(current_database(), 'TEMP') AS database_ddl,
               has_parameter_privilege('session_replication_role', 'SET') AS bypass,
               EXISTS (SELECT FROM pg_namespace n WHERE
                 n.nspname = ANY(current_schemas(true)) AND
                 has_schema_privilege(n.oid, 'CREATE')) AS schema_ddl,
               EXISTS (SELECT FROM pg_class c WHERE
                 c.relnamespace = current_schema()::regnamespace AND
                 (c.relowner = r.oid OR (c.relkind IN ('r','p','v','m','f') AND
                   (has_table_privilege(c.oid, 'TRUNCATE') OR
                    has_table_privilege(c.oid, 'TRIGGER'))))) AS table_ddl,
               EXISTS (SELECT FROM pg_proc p WHERE
                 (p.pronamespace = current_schema()::regnamespace AND p.proowner = r.oid)
                 OR (has_schema_privilege(p.pronamespace, 'USAGE') AND p.prosecdef AND
                   has_function_privilege(p.oid, 'EXECUTE'))) AS function_ddl
        FROM pg_roles r WHERE r.rolname = current_user
    """)
            )
        )
        .mappings()
        .one()
    )
    if (
        row["database"] != config.database_name
        or row["schema"] != config.database_schema
        or row["role"] != config.database_role
        or row["login"] != config.database_role
        or list(row["schemas"]) != [config.database_schema]
        or row["replication"] != "origin"
        or any(
            row[key]
            for key in (
                "elevated",
                "membership",
                "database_ddl",
                "bypass",
                "schema_ddl",
                "table_ddl",
                "function_ddl",
            )
        )
    ):
        raise ValueError("HOST_DATABASE_UNSAFE")
    await verify_barrier(session)


async def no_work_history(session: AsyncSession) -> bool:
    # Constants owned by the migration, never caller-supplied identifiers.
    for table in GUARDED_TABLES:
        if await session.scalar(text(f"SELECT EXISTS (SELECT FROM {table})")):
            return False
    return True
