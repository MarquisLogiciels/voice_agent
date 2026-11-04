"""Load measurements (opt-in: ``pytest tests/load --run-load -s``).

These measure *platform overhead* (orchestration, tools, persistence, API) on this machine.
Provider latency is simulated (ScriptedLLM ttft) and audio transport is not exercised; real
capacity per worker must be measured against LiveKit with real providers (see
docs/operations.md → capacity testing).
"""

from __future__ import annotations

import asyncio
import json
import os
import time
import uuid
from pathlib import Path
from typing import Any

import httpx
import psutil

from tests.evaluations.test_evaluation_and_optimization import eval_config
from tests.support.models import receptionist_model
from voice_platform.evaluation.scenario import load_scenarios
from voice_platform.evaluation.simulator import run_scenario
from voice_platform.observability.stats import summarize

ROOT = Path(__file__).resolve().parents[2]
RESULTS = ROOT / "docs" / "load-test-results.json"


class LoopLag:
    def __init__(self) -> None:
        self.samples: list[float] = []
        self._task: asyncio.Task[None] | None = None

    async def _run(self) -> None:
        while True:
            t = time.perf_counter()
            await asyncio.sleep(0.05)
            self.samples.append(time.perf_counter() - t - 0.05)

    def start(self) -> None:
        self._task = asyncio.create_task(self._run())

    def stop(self) -> dict[str, Any]:
        assert self._task
        self._task.cancel()
        return summarize(self.samples)


def _record(name: str, data: dict[str, Any]) -> None:
    existing = json.loads(RESULTS.read_text()) if RESULTS.exists() else {}
    existing[name] = {
        **data,
        "cpu_count": os.cpu_count(),
        "measured_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    RESULTS.parent.mkdir(exist_ok=True)
    RESULTS.write_text(json.dumps(existing, indent=2))
    print(json.dumps({name: existing[name]}, indent=2))


async def test_concurrent_conversations_overhead() -> None:
    lib = {s.id: s for s in load_scenarios(ROOT / "scenarios" / "receptionist")}
    scenario = lib["book-simple"]
    cfg = eval_config()
    proc = psutil.Process()
    rss_before = proc.memory_info().rss
    cpu_before = proc.cpu_times()
    for concurrency in (10, 50, 100):
        lag = LoopLag()
        lag.start()
        started = time.perf_counter()

        def model() -> Any:
            m = receptionist_model(competent=True)
            m.ttft_s = 0.25  # simulated provider TTFT
            return m

        results = await asyncio.gather(
            *(run_scenario(scenario, cfg, agent_llm=model()) for _ in range(concurrency))
        )
        wall = time.perf_counter() - started
        lag_stats = lag.stop()
        turns = [t.latency_s for r in results for t in r.turns]
        failures = sum(1 for r in results if r.error or len(r.sandbox.state.appointments) != 1)
        cpu_after = proc.cpu_times()
        _record(
            f"runtime_concurrency_{concurrency}",
            {
                "concurrent_conversations": concurrency,
                "turns": len(turns),
                "wall_s": round(wall, 3),
                "turn_latency_s": summarize(turns),
                "platform_overhead_per_turn_s_p95": round(
                    (summarize(turns)["p95"] or 0) - 2 * 0.25, 4
                ),
                "event_loop_lag_s": lag_stats,
                "rss_mb": round(proc.memory_info().rss / 1e6, 1),
                "rss_growth_mb": round((proc.memory_info().rss - rss_before) / 1e6, 1),
                "cpu_s": round(
                    (cpu_after.user + cpu_after.system) - (cpu_before.user + cpu_before.system), 2
                ),
                "failed_conversations": failures,
            },
        )
        assert failures == 0


async def test_api_throughput(db: Any, tmp_path: Path) -> None:
    from tests.conftest import sample_config
    from voice_platform.apps.api.main import create_app
    from voice_platform.apps.services import Services
    from voice_platform.core.db import DbScope
    from voice_platform.core.models import Membership, Tenant, User
    from voice_platform.shared.crypto import hash_password
    from voice_platform.shared.settings import Settings

    settings = Settings(
        environment="test", blob_local_path=str(tmp_path), api_rate_limit_per_minute=1_000_000
    )
    services = Services.from_settings(settings, db=db)
    email = f"load-{uuid.uuid4().hex[:6]}@example.com"
    async with db.session(DbScope.system()) as s:
        t = Tenant(slug=f"load-{uuid.uuid4().hex[:6]}", name="Load")
        u = User(email=email, password_hash=hash_password("correct-horse-battery"))
        s.add_all([t, u])
        await s.flush()
        s.add(Membership(tenant_id=t.id, user_id=u.id, role="owner"))
        tid = t.id
    app = create_app(services=services)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        tok = (
            await c.post(
                "/v1/auth/login", json={"email": email, "password": "correct-horse-battery"}
            )
        ).json()["access_token"]
        h = {"Authorization": f"Bearer {tok}"}
        await c.post(
            f"/v1/tenants/{tid}/agents",
            headers=h,
            json={"slug": "a", "name": "A", "config": sample_config()},
        )

        async def timed(method: str, path: str, **kw: Any) -> float:
            t0 = time.perf_counter()
            r = await c.request(method, path, headers=h, **kw)
            assert r.status_code < 400, r.text
            return time.perf_counter() - t0

        for concurrency in (1, 10, 20, 50):
            sem = asyncio.Semaphore(concurrency)  # exactly `concurrency` requests in flight

            async def bounded(sem: asyncio.Semaphore = sem) -> float:
                async with sem:
                    return await timed("GET", f"/v1/tenants/{tid}/agents")

            started = time.perf_counter()
            lat = await asyncio.gather(*(bounded() for _ in range(max(50, concurrency * 10))))
            wall = time.perf_counter() - started
            _record(
                f"api_list_agents_c{concurrency}",
                {
                    "in_flight": concurrency,
                    "requests": len(lat),
                    "wall_s": round(wall, 3),
                    "throughput_rps": round(len(lat) / wall, 1),
                    "latency_s": summarize(lat),
                    "db_pool_size": 10,
                    "transport": "in-process ASGI (no network)",
                },
            )
