"""Agent worker: a LiveKit AgentServer that executes calls for every tenant.

Run: ``vp-worker start`` (production) · ``vp-worker dev`` (hot reload) · see LiveKit CLI.
"""

from __future__ import annotations

import os
import urllib.request
from typing import Any
from urllib.parse import urlparse

from livekit.agents import AgentServer, JobContext, JobProcess, cli

from ...shared.logging import configure_logging, get_logger, install_redaction
from ...shared.settings import get_settings
from ..services import Services
from .ecs_protection import TaskProtection

logger = get_logger(__name__)
_services: Services | None = None


def _get_services() -> Services:
    global _services
    if _services is None:
        settings = get_settings()
        # each job process gets a small pool; many processes run per worker host
        settings.db_pool_size = min(settings.db_pool_size, 3)
        settings.db_max_overflow = min(settings.db_max_overflow, 2)
        _services = Services.from_settings(settings)
    return _services


def prewarm(proc: JobProcess) -> None:
    settings = get_settings()
    configure_logging(
        level=settings.log_level,
        json_output=settings.log_json,
        service="vp-worker",
        log_pii=settings.log_pii,
    )
    from ...observability.tracing_setup import configure_tracing

    configure_tracing(
        service="vp-worker",
        endpoint=settings.otel_exporter_otlp_endpoint,
        environment=settings.environment.value,
    )
    try:
        from livekit.plugins import silero

        proc.userdata["vad"] = silero.VAD.load()
    except Exception:  # VAD is optional per agent config; log and continue
        logger.warning("could not preload Silero VAD", exc_info=True)
    _get_services()


def worker_http_proxy(ws_url: str) -> str | None:
    """Proxy for the worker's LiveKit connection.

    ``AgentServer`` uses ``HTTPS_PROXY``/``HTTP_PROXY`` unconditionally and ignores
    ``NO_PROXY``, so a LiveKit server on an internal network becomes unreachable behind an
    egress proxy. Honour ``NO_PROXY`` here and pass the result explicitly.
    """
    host = urlparse(ws_url).hostname or ""
    if host and urllib.request.proxy_bypass(host):
        return None
    return os.environ.get("HTTPS_PROXY") or os.environ.get("HTTP_PROXY") or None


def register_main_process_plugins(enabled: bool) -> bool:
    """LiveKit's turn detector runs in the worker's shared inference process, whose runners
    are registered when the plugin module is *imported in the main process* (the same import
    makes ``download-files`` fetch the model at image build). Importing it lazily inside job
    processes — as the provider factory does — left the runner unregistered, so model-based
    turn detection could not work in production."""
    if not enabled:
        return False
    try:
        import livekit.plugins.turn_detector.english
        import livekit.plugins.turn_detector.multilingual  # noqa: F401
    except ImportError:
        logger.warning("turn-detector plugin not installed; model turn detection unavailable")
        return False
    return True


def build_server() -> AgentServer:
    settings = get_settings()
    register_main_process_plugins(settings.worker_turn_detector)
    ws_url = os.environ.get("LIVEKIT_URL") or settings.livekit_url
    server = AgentServer(
        http_proxy=worker_http_proxy(ws_url),
        setup_fnc=prewarm,
        prometheus_port=settings.worker_metrics_port,
        job_memory_warn_mb=700,
        drain_timeout=1800,
    )

    # LiveKit's CLI installs its own log handler in the main process, which also re-emits every
    # job-process record; redact there too (transcripts, phone numbers in room names).
    protection = TaskProtection(lambda: len(server.active_jobs))

    def _started() -> None:
        install_redaction(log_pii=settings.log_pii)
        protection.start()  # no-op outside ECS

    server.on("worker_started", _started)
    # The entrypoint must be importable by qualified name: in production LiveKit runs each
    # job in a separate process (forkserver) and pickles the function reference.
    server.rtc_session(agent_name=settings.agent_dispatch_name)(entrypoint)
    return server


async def entrypoint(ctx: JobContext) -> None:
    from .call import CallHandler

    vad: Any = ctx.proc.userdata.get("vad")
    await CallHandler(ctx, _get_services(), vad=vad).run()


def run() -> None:
    cli.run_app(build_server())


if __name__ == "__main__":
    run()
