"""Alembic environment. Migrations run as the schema owner role
(``VP_MIGRATION_DATABASE_URL``, falling back to ``VP_DATABASE_URL``)."""

from __future__ import annotations

import asyncio
import os

from alembic import context
from sqlalchemy.ext.asyncio import create_async_engine

from voice_platform.core.models import Base

target_metadata = Base.metadata


def _url() -> str:
    url = context.config.attributes.get("url") or os.environ.get(
        "VP_MIGRATION_DATABASE_URL"
    ) or os.environ.get("VP_DATABASE_URL")
    if not url:
        raise RuntimeError("set VP_MIGRATION_DATABASE_URL (schema owner role)")
    return str(url)


def run_offline() -> None:
    context.configure(url=_url(), target_metadata=target_metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()


def _run(connection) -> None:  # type: ignore[no-untyped-def]
    context.configure(connection=connection, target_metadata=target_metadata,
                      compare_type=True, transaction_per_migration=True)
    with context.begin_transaction():
        context.run_migrations()


async def run_online() -> None:
    engine = create_async_engine(_url())
    async with engine.connect() as conn:
        await conn.run_sync(_run)
        await conn.commit()
    await engine.dispose()


if context.is_offline_mode():
    run_offline()
else:
    asyncio.run(run_online())
