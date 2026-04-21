"""Database layer: models, session factory, repositories, and typed enums.

The module is deliberately split so pure enum imports (used widely across the
codebase) don't drag in SQLAlchemy. Import from ``awf.db.enums`` when you only
need the status enums; import from ``awf.db.models`` when you need ORM classes.
"""
