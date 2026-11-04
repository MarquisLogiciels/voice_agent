"""Lab fixtures: real LiveKit server + worker subprocess + local models.

Opt-in: ``pytest tests/lab --run-lab``. Start the services first:
``docker compose -f infrastructure/lab/docker-compose.lab.yml up -d``.
"""

from __future__ import annotations

import os
import signal
import socket
import subprocess
import sys
import time
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from tests.lab.support import (
    LIVEKIT_KEY,
    LIVEKIT_SECRET,
    LIVEKIT_URL,
    OLLAMA_URL,
    VOSK_EN,
    VOSK_FR,
    lab_available,
)

ROOT = Path(__file__).resolve().parents[2]
EVIDENCE = ROOT / "docs" / "evidence"


@pytest.fixture(scope="session", autouse=True)
def _require_lab() -> None:
    missing = lab_available()
    if missing:
        pytest.skip(f"lab services unavailable: {missing}")


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@pytest.fixture(scope="session")
def evidence_dir() -> Path:
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    return EVIDENCE


@pytest.fixture(scope="session")
def worker(
    pg_urls: dict[str, str], tmp_path_factory: pytest.TempPathFactory
) -> Iterator[dict[str, Any]]:
    """The real ``vp-worker start`` process registered with the lab LiveKit server."""
    agent_name = f"vp-lab-{uuid.uuid4().hex[:6]}"
    log_path = tmp_path_factory.mktemp("worker") / "worker.log"
    env = {
        **os.environ,
        "VP_ENVIRONMENT": "dev",
        "VP_DATABASE_URL": pg_urls["app"],
        "VP_LOG_JSON": "true",
        "VP_AGENT_DISPATCH_NAME": agent_name,
        "VP_WORKER_METRICS_PORT": str(_free_port()),
        "VP_TRUSTED_PROVIDER_ENDPOINTS": f'["{OLLAMA_URL}", "{VOSK_EN}", "{VOSK_FR}"]',
        "VP_TELEPHONY_DRY_RUN": "false",
        "LIVEKIT_URL": LIVEKIT_URL,
        "LIVEKIT_API_KEY": LIVEKIT_KEY,
        "LIVEKIT_API_SECRET": LIVEKIT_SECRET,
        "VP_LIVEKIT_URL": LIVEKIT_URL,
        "VP_LIVEKIT_API_KEY": LIVEKIT_KEY,
        "VP_LIVEKIT_API_SECRET": LIVEKIT_SECRET,
        "PYTHONPATH": str(ROOT / "src"),
    }
    log = log_path.open("w")
    proc = subprocess.Popen(
        [sys.executable, "-m", "voice_platform.apps.worker.main", "start"],
        env=env,
        stdout=log,
        stderr=subprocess.STDOUT,
        cwd=str(ROOT),
    )
    deadline = time.time() + 90
    while time.time() < deadline:
        text = log_path.read_text()
        if "registered worker" in text:
            break
        if proc.poll() is not None:
            raise RuntimeError(f"worker exited early:\n{text[-3000:]}")
        time.sleep(0.5)
    else:
        proc.kill()
        raise RuntimeError(f"worker did not register:\n{log_path.read_text()[-3000:]}")
    info = {"agent_name": agent_name, "log": log_path, "proc": proc}
    yield info
    proc.send_signal(signal.SIGTERM)
    try:
        info["exit_code"] = proc.wait(timeout=60)
    except subprocess.TimeoutExpired:
        proc.kill()
        info["exit_code"] = "killed"
    log.close()
