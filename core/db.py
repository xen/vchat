from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from core.settings import config

# Connection pool setup
engine = create_async_engine(
    config["database_uri"],
    echo=config["sql_echo"],
    pool_size=5,
    max_overflow=10,
)

# Session factory
async_session_factory = async_sessionmaker(engine, expire_on_commit=False)


class Base(DeclarativeBase):
    __abstract__ = True
