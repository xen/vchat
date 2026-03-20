import asyncio

# add your model's MetaData object here
# for 'autogenerate' support
import importlib.util
from logging.config import fileConfig

import sqlalchemy as sa
from alembic import context
from sqlalchemy.ext.asyncio import create_async_engine

if not importlib.util.find_spec("vchat"):
    import sys
    from pathlib import Path

    sys.path.append(str(Path(__file__).parent.parent.absolute()))


from vchat.models.base import DateTime_, Base

target_metadata = Base.metadata
from vchat.settings import config as core_conf

sa.DateTime_ = DateTime_

# this is the Alembic Config object, which provides
# access to the values within the .ini file in use.
config = context.config

# Interpret the config file for Python logging.
# This line sets up loggers basically.
fileConfig(config.config_file_name)


def exclude_tables_from_config(config_):
    tables = {
        "celery_taskmeta",
        "celery_tasksetmeta",
    }
    tables_ = config_.get("tables", None)
    if tables_ is not None:
        tables.update([i.strip() for i in tables_.split(",")])
    return tables


exclude_tables = exclude_tables_from_config(config.get_section("alembic:exclude"))


def include_object(obj, name, type_, reflected, compare_to):  # noqa: ARG001
    if type_ == "table" and name in exclude_tables or obj.info.get("is_view", False):
        return False
    return True


def run_migrations_offline():
    """Run migrations in 'offline' mode.

    This configures the context with just a URL
    and not an Engine, though an Engine is acceptable
    here as well.  By skipping the Engine creation
    we don't even need a DBAPI to be available.

    Calls to context.execute() here emit the given string to the
    script output.

    """
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        include_object=include_object,
    )

    with context.begin_transaction():
        context.run_migrations()


def do_migrations_online(connection):
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        include_object=include_object,
    )

    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online():
    """Run migrations in 'online' mode.

    In this scenario we need to create an Engine
    and associate a connection with the context.

    """
    connectable = create_async_engine(core_conf["database_uri"], echo=True)

    async with connectable.connect() as connection:
        await connection.run_sync(do_migrations_online)

    await connectable.dispose()


if context.is_offline_mode():
    print("Running migrations in offline mode")
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
