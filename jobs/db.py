"""
Utilities for working with synchronous database connections inside the jobs package.
"""

from __future__ import annotations

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine

from core.settings import config


def sync_db_uri(uri: str | None = None) -> str:
    """
    Convert an async SQLAlchemy URI into its synchronous counterpart.
    """
    uri = uri or config["database_uri"]
    if "+asyncpg" in uri:
        return uri.replace("+asyncpg", "+psycopg", 1)
    return uri


def create_sync_engine(uri: str | None = None, **kwargs) -> Engine:
    """
    Create a synchronous SQLAlchemy engine that can be used inside Celery jobs.
    """
    return create_engine(sync_db_uri(uri), future=True, **kwargs)
