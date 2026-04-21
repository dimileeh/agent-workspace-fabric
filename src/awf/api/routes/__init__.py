"""Route modules for the AWF FastAPI app.

Each sub-module defines a ``router`` (FastAPI ``APIRouter``) that ``create_app``
mounts. Keep route handlers thin: delegate business logic to services in
``awf.control``, ``awf.node``, and ``awf.db``.
"""
