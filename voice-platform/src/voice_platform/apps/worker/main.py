"""Agent worker: a LiveKit AgentServer that executes calls for every tenant.

Run: ``vp-worker start`` (production) · ``vp-worker dev`` (hot reload) · see LiveKit CLI.
"""

from __future__ import annotations

from typing import Any

from livekit.agents import AgentServer, JobContext, JobProcess, cli

from ...shared.logging import configure_logging, get_logger
from ...shared.settings import get_settings
from ..services import Services

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
    configure_logging(level=settings.log_level, json_output=settings.log_json, service="vp-worker")
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


def build_server() -> AgentServer:
    settings = get_settings()
    server = AgentServer(
        setup_fnc=prewarm,
        prometheus_port=settings.worker_metrics_port,
        job_memory_warn_mb=700,
        drain_timeout=1800,
    )

    @server.rtc_session(agent_name=settings.agent_dispatch_name)
    async def entrypoint(ctx: JobContext) -> None:
        from .call import CallHandler

        vad: Any = ctx.proc.userdata.get("vad")
        await CallHandler(ctx, _get_services(), vad=vad).run()

    return server


def run() -> None:
    cli.run_app(build_server())


if __name__ == "__main__":
    run()
