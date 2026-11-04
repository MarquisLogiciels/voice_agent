from __future__ import annotations

import os
import sys
import uuid
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

os.environ.setdefault("VP_ENVIRONMENT", "test")
os.environ.setdefault("VP_LOG_JSON", "false")
os.environ.setdefault("VP_ENCRYPTION_KEYS", "R4n4fW3Yr6t6qQdC7bJ0m3kq9vJb8h0pZp5S6yT0pXk=")


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption("--run-load", action="store_true", help="run load tests")
    parser.addoption("--run-live", action="store_true", help="run tests needing real credentials")


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    for item in items:
        parts = Path(str(item.fspath)).parts
        for category in ("unit", "integration", "e2e", "load", "evaluations"):
            if category in parts:
                item.add_marker(
                    getattr(pytest.mark, "e2e" if category == "evaluations" else category)
                )
        if "load" in parts and not config.getoption("--run-load"):
            item.add_marker(pytest.mark.skip(reason="load tests need --run-load"))
        if item.get_closest_marker("live") and not config.getoption("--run-live"):
            item.add_marker(pytest.mark.skip(reason="live tests need --run-live"))


# ------------------------------------------------------------------------------------------
# PostgreSQL
# ------------------------------------------------------------------------------------------


@pytest.fixture(scope="session")
def pg_urls() -> Iterator[dict[str, str]]:
    """Owner + app URLs for a migrated database.

    Uses ``VP_TEST_PG_OWNER_URL``/``VP_TEST_PG_APP_URL`` when provided (CI service container),
    otherwise starts a throwaway local cluster.
    """
    owner = os.environ.get("VP_TEST_PG_OWNER_URL")
    app = os.environ.get("VP_TEST_PG_APP_URL")
    pg = None
    if not (owner and app):
        from tests.support.pg import LocalPostgres

        pg = LocalPostgres.start()
        owner, app = pg.url("vp_owner"), pg.url("vp_app")
    from alembic import command
    from alembic.config import Config

    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "migrations"))
    cfg.attributes["url"] = owner
    command.upgrade(cfg, "head")
    try:
        yield {"owner": owner, "app": app}
    finally:
        if pg is not None:
            pg.stop()


@pytest_asyncio.fixture(scope="session")
async def db(pg_urls: dict[str, str]) -> AsyncIterator[Any]:
    from sqlalchemy.ext.asyncio import create_async_engine

    from voice_platform.core.db import Database

    engine = create_async_engine(pg_urls["app"], pool_size=10, max_overflow=10)
    database = Database(engine)
    yield database
    await database.dispose()


# ------------------------------------------------------------------------------------------
# Domain fixtures
# ------------------------------------------------------------------------------------------


def sample_config(**overrides: Any) -> dict[str, Any]:
    from voice_platform.config.schema import deep_merge

    base: dict[str, Any] = {
        "name": "Front desk",
        "language": "en",
        "instructions": "Help callers book appointments and answer questions about the clinic.",
        "business": {
            "name": "Maple Dental",
            "timezone": "America/Toronto",
            "hours": {
                "schedule": {d: ["09:00-17:00"] for d in ("mon", "tue", "wed", "thu", "fri")}
            },
            "appointments": {
                "default_duration_min": 30,
                "min_notice_min": 0,
                "hours": {
                    "schedule": {d: ["09:00-17:00"] for d in ("mon", "tue", "wed", "thu", "fri")}
                },
            },
        },
        "greeting": {"mode": "say", "text": "Hi, thanks for calling Maple Dental!"},
        "pipeline": {
            "mode": "cascaded",
            "stt": {"provider": "deepgram", "model": "nova-3", "language": "en"},
            "llm": {"provider": "openai", "model": "gpt-4.1-mini", "temperature": 0.3},
            "tts": {"provider": "cartesia", "model": "sonic-2", "voice": "voice-123"},
            "vad": {"provider": "silero"},
            "turn_detection": {"mode": "model", "provider": "livekit", "model": "multilingual"},
        },
    }
    return deep_merge(base, overrides)


@pytest.fixture
def config_factory() -> Any:
    return sample_config


@pytest_asyncio.fixture
async def tenant(db: Any) -> Any:
    """A fresh tenant per test (isolation between tests without truncation)."""
    from voice_platform.core.db import DbScope
    from voice_platform.core.models import Tenant

    async with db.session(DbScope.system()) as s:
        t = Tenant(slug=f"t-{uuid.uuid4().hex[:10]}", name="Test Tenant")
        s.add(t)
        await s.flush()
        return t


@pytest_asyncio.fixture
async def other_tenant(db: Any) -> Any:
    from voice_platform.core.db import DbScope
    from voice_platform.core.models import Tenant

    async with db.session(DbScope.system()) as s:
        t = Tenant(slug=f"o-{uuid.uuid4().hex[:10]}", name="Other Tenant")
        s.add(t)
        await s.flush()
        return t
