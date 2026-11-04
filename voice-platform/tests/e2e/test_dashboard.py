"""Browser smoke test: the dashboard drives real API endpoints (Playwright + Chromium)."""

from __future__ import annotations

import asyncio
import json
import socket
import threading
import time
import uuid
from pathlib import Path
from typing import Any

import pytest

from tests.conftest import sample_config

PASSWORD = "correct-horse-battery-staple"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@pytest.fixture
def server(pg_urls: dict[str, str], tmp_path: Path) -> Any:
    """Run the real API under uvicorn in a background thread with its own event loop."""
    import uvicorn
    from sqlalchemy.ext.asyncio import create_async_engine

    from voice_platform.apps.api.main import create_app
    from voice_platform.apps.services import Services
    from voice_platform.core.db import Database, DbScope
    from voice_platform.core.models import Membership, Tenant, User
    from voice_platform.shared.crypto import hash_password
    from voice_platform.shared.settings import Settings

    port = _free_port()
    email = f"owner-{uuid.uuid4().hex[:8]}@example.com"
    settings = Settings(environment="test", blob_local_path=str(tmp_path / "b"),
                        public_base_url=f"http://127.0.0.1:{port}")
    holder: dict[str, Any] = {}

    def run() -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        db = Database(create_async_engine(pg_urls["app"]))

        async def seed() -> None:
            async with db.session(DbScope.system()) as s:
                t = Tenant(slug=f"ui-{uuid.uuid4().hex[:6]}", name="UI Dental")
                u = User(email=email, password_hash=hash_password(PASSWORD))
                s.add_all([t, u])
                await s.flush()
                s.add(Membership(tenant_id=t.id, user_id=u.id, role="owner"))

        loop.run_until_complete(seed())
        app = create_app(services=Services.from_settings(settings, db=db))
        config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning",
                                loop="asyncio")
        srv = uvicorn.Server(config)
        holder["server"] = srv
        loop.run_until_complete(srv.serve())
        loop.run_until_complete(db.dispose())

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    for _ in range(100):
        if holder.get("server") is not None and holder["server"].started:
            break
        time.sleep(0.1)
    yield {"url": f"http://127.0.0.1:{port}", "email": email}
    holder["server"].should_exit = True
    thread.join(timeout=10)


def test_dashboard_login_create_agent_deploy(server: dict[str, Any]) -> None:
    import os
    import shutil
    import subprocess

    if shutil.which("node") is None:
        pytest.skip("node is required for the browser smoke test")
    script = Path(__file__).with_name("dashboard_smoke.cjs")
    env = {**os.environ, "VP_URL": server["url"], "VP_EMAIL": server["email"],
           "VP_PASSWORD": PASSWORD, "VP_CONFIG": json.dumps(sample_config())}
    proc = subprocess.run(["node", str(script)], env=env, capture_output=True, text=True,
                          timeout=120)
    assert proc.returncode == 0, proc.stdout + proc.stderr
