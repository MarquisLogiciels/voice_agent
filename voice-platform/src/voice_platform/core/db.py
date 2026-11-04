"""Database engine, sessions and the row-level-security context.

Every unit of work runs in one transaction whose first statements set
``app.tenant_id`` / ``app.platform_admin`` / ``app.user_id`` with ``set_config(..., true)``
(transaction-local). RLS policies read those settings, so a query that forgets to filter by
tenant still cannot see another tenant's rows.
"""

from __future__ import annotations

import contextlib
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass

import sqlalchemy as sa
from sqlalchemy.exc import DBAPIError, OperationalError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from ..shared.errors import DependencyUnavailableError
from ..shared.logging import get_logger
from ..shared.settings import Settings

logger = get_logger(__name__)


@dataclass(frozen=True)
class DbScope:
    """Who the transaction acts for. Exactly one of tenant / platform should normally be set."""

    tenant_id: uuid.UUID | None = None
    platform: bool = False
    user_id: uuid.UUID | None = None

    @classmethod
    def for_tenant(cls, tenant_id: uuid.UUID, user_id: uuid.UUID | None = None) -> DbScope:
        return cls(tenant_id=tenant_id, user_id=user_id)

    @classmethod
    def system(cls) -> DbScope:
        return cls(platform=True)


class Database:
    def __init__(self, engine: AsyncEngine, *, statement_timeout_ms: int = 15_000) -> None:
        self.engine = engine
        self._sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
        self._statement_timeout_ms = statement_timeout_ms

    @classmethod
    def from_settings(cls, settings: Settings) -> Database:
        engine = create_async_engine(
            settings.database_url.get_secret_value(),
            pool_size=settings.db_pool_size,
            max_overflow=settings.db_max_overflow,
            pool_timeout=settings.db_pool_timeout_s,
            pool_pre_ping=True,
            pool_recycle=1800,
            connect_args={"server_settings": {"application_name": settings.service_name}},
        )
        return cls(engine, statement_timeout_ms=settings.db_statement_timeout_ms)

    @contextlib.asynccontextmanager
    async def session(self, scope: DbScope) -> AsyncIterator[AsyncSession]:
        """Open a transaction bound to ``scope``. Commits on success, rolls back on error."""
        try:
            session = self._sessionmaker()
        except (OperationalError, OSError) as e:  # pragma: no cover - engine creation
            raise DependencyUnavailableError("database unavailable") from e
        try:
            async with session.begin():
                await session.execute(
                    sa.text(
                        "select set_config('app.tenant_id', :tenant, true),"
                        " set_config('app.platform_admin', :platform, true),"
                        " set_config('app.user_id', :user, true),"
                        " set_config('statement_timeout', :timeout, true)"
                    ),
                    {
                        "tenant": str(scope.tenant_id) if scope.tenant_id else "",
                        "platform": "on" if scope.platform else "off",
                        "user": str(scope.user_id) if scope.user_id else "",
                        "timeout": str(self._statement_timeout_ms),
                    },
                )
                yield session
        except (OperationalError, ConnectionError, OSError) as e:
            raise DependencyUnavailableError("database unavailable") from e
        except DBAPIError as e:
            if e.connection_invalidated:
                raise DependencyUnavailableError("database connection lost") from e
            raise
        finally:
            await session.close()

    async def ping(self) -> bool:
        try:
            async with self.engine.connect() as conn:
                await conn.execute(sa.text("select 1"))
            return True
        except Exception:
            logger.warning("database ping failed", exc_info=True)
            return False

    async def dispose(self) -> None:
        await self.engine.dispose()
