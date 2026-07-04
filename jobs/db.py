"""
Utilities for working with synchronous database connections inside the jobs package.
"""

from __future__ import annotations

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine

from vchat.settings import cfg


def sync_db_uri() -> str:
    """
    Convert an async SQLAlchemy URI into its synchronous counterpart.
    """
    if "+asyncpg" in cfg.database_uri:
        return cfg.database_uri.replace("+asyncpg", "+psycopg", 1)
    return cfg.database_uri


def create_sync_engine(**kwargs) -> Engine:
    """
    Create a synchronous SQLAlchemy engine that can be used inside Celery jobs.
    """
    return create_engine(sync_db_uri(), future=True, **kwargs)
