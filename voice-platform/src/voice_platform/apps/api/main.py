"""FastAPI application: middleware, error mapping, health, metrics, dashboard."""

import time
import uuid
from contextlib import asynccontextmanager
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from ...observability.metrics import API_LATENCY, API_REQUESTS
from ...shared.errors import PlatformError, RateLimitedError
from ...shared.logging import configure_logging, get_logger, log_context
from ...shared.settings import Settings, get_settings
from ..services import Services
from . import routes_agents, routes_core, routes_knowledge_eval, routes_ops

logger = get_logger(__name__)
DASHBOARD_DIR = Path(__file__).resolve().parents[2] / "dashboard"
MAX_BODY = 30 * 1024 * 1024


def create_app(services: Services | None = None, settings: Settings | None = None) -> FastAPI:
    settings = settings or (services.settings if services else get_settings())

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        if not hasattr(app.state, "services"):
            app.state.services = Services.from_settings(settings)
        yield
        if getattr(app.state, "owns_services", True):
            await app.state.services.aclose()

    app = FastAPI(title="Voice Platform API", version="0.1.0", lifespan=lifespan,
                  docs_url="/docs", redoc_url=None)
    if services is not None:
        app.state.services = services
        app.state.owns_services = False

    if settings.cors_origins:
        app.add_middleware(CORSMiddleware, allow_origins=settings.cors_origins,
                           allow_methods=["*"], allow_headers=["Authorization", "Content-Type"],
                           allow_credentials=False)

    @app.middleware("http")
    async def request_context(request: Request,
                              call_next: Callable[[Request], Awaitable[Response]]) -> Response:
        request_id = request.headers.get("x-request-id") or uuid.uuid4().hex
        declared = request.headers.get("content-length")
        if declared and declared.isdigit() and int(declared) > MAX_BODY:
            return JSONResponse({"error": {"code": "payload_too_large"}}, status_code=413)
        started = time.perf_counter()
        route = "unmatched"
        client_ip = request.client.host if request.client else None
        with log_context(request_id=request_id, client_ip=client_ip):
            try:
                response = await call_next(request)
            except Exception:
                logger.exception("unhandled error")
                response = JSONResponse({"error": {"code": "internal_error",
                                                   "request_id": request_id}}, status_code=500)
            matched = request.scope.get("route")
            if matched is not None:
                route = getattr(matched, "path", route)
            elapsed = time.perf_counter() - started
            API_REQUESTS.labels(request.method, route, str(response.status_code)).inc()
            API_LATENCY.labels(request.method, route).observe(elapsed)
            if response.status_code >= 500:
                logger.error("request failed", extra={"route": route,
                                                      "status": response.status_code})
        response.headers["x-request-id"] = request_id
        response.headers["x-content-type-options"] = "nosniff"
        response.headers["referrer-policy"] = "no-referrer"
        response.headers["x-frame-options"] = "DENY"
        if request.url.path.startswith("/dashboard"):
            response.headers["content-security-policy"] = (
                "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
                "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'")
        return response

    @app.exception_handler(PlatformError)
    async def platform_error(request: Request, exc: PlatformError) -> JSONResponse:
        headers = {}
        if isinstance(exc, RateLimitedError) and exc.retry_after_s:
            headers["Retry-After"] = str(int(exc.retry_after_s) + 1)
        return JSONResponse({"error": {"code": exc.code, "message": exc.message,
                                       "details": exc.details}},
                            status_code=exc.http_status, headers=headers)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        errors = [{"loc": list(e["loc"]), "msg": e["msg"]} for e in exc.errors()[:20]]
        return JSONResponse({"error": {"code": "validation_failed", "message": "invalid request",
                                       "details": {"errors": errors}}}, status_code=422)

    @app.get("/healthz", include_in_schema=False)
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/readyz", include_in_schema=False)
    async def readyz(request: Request) -> Response:
        svc: Services = request.app.state.services
        checks: dict[str, Any] = {"database": await svc.db.ping()}
        if svc.redis is not None:
            try:
                checks["redis"] = bool(await svc.redis.ping())
            except Exception:
                checks["redis"] = False  # degraded, not fatal (rate limiter falls back)
        ready = checks["database"]
        return JSONResponse({"ready": ready, "checks": checks}, status_code=200 if ready else 503)

    @app.get("/metrics", include_in_schema=False)
    async def metrics() -> Response:
        return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

    for r in (routes_core.router, routes_agents.router, routes_ops.router, routes_ops.public,
              routes_knowledge_eval.router):
        app.include_router(r)

    if DASHBOARD_DIR.exists():
        app.mount("/dashboard", StaticFiles(directory=DASHBOARD_DIR, html=True), name="dashboard")

        @app.get("/", include_in_schema=False)
        async def root() -> RedirectResponse:
            return RedirectResponse("/dashboard/")

    return app


def run() -> None:
    import uvicorn

    settings = get_settings()
    configure_logging(level=settings.log_level, json_output=settings.log_json, service="vp-api")
    uvicorn.run(create_app(settings=settings), host="0.0.0.0", port=8000,  # noqa: S104
                proxy_headers=True, forwarded_allow_ips="*", log_config=None,
                timeout_graceful_shutdown=20)
