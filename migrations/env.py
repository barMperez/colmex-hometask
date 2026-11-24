import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy.engine import Connection
from sqlalchemy.pool import NullPool

from rates.adapters.db.session import create_engine
from rates.adapters.db.tables import metadata
from rates.settings import Settings

config = context.config

if config.config_file_name is not None and config.attributes.get("configure_logging", True):
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = metadata


def _database_url() -> str:
    return config.get_main_option("sqlalchemy.url") or Settings().database_url


def _do_run_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        render_as_batch=connection.dialect.name == "sqlite",
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


async def _run_async_migrations() -> None:
    engine = create_engine(_database_url(), poolclass=NullPool)
    try:
        async with engine.connect() as connection:
            await connection.run_sync(_do_run_migrations)
            await connection.commit()
    finally:
        await engine.dispose()


if context.is_offline_mode():
    context.configure(url=_database_url(), target_metadata=target_metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()
else:
    asyncio.run(_run_async_migrations())
