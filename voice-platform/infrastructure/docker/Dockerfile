# syntax=docker/dockerfile:1.7
# Build context: repository root (the platform resolves LiveKit from ../livekit-agents during
# development). Build: docker build -f voice-platform/infrastructure/docker/Dockerfile -t vp .
FROM python:3.12-slim AS build
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy PIP_DISABLE_PIP_VERSION_CHECK=1
RUN pip install --no-cache-dir "uv>=0.5"
WORKDIR /src
COPY livekit-agents ./livekit-agents
COPY livekit-plugins/livekit-plugins-openai ./livekit-plugins/livekit-plugins-openai
COPY livekit-plugins/livekit-plugins-anthropic ./livekit-plugins/livekit-plugins-anthropic
COPY livekit-plugins/livekit-plugins-google ./livekit-plugins/livekit-plugins-google
COPY livekit-plugins/livekit-plugins-deepgram ./livekit-plugins/livekit-plugins-deepgram
COPY livekit-plugins/livekit-plugins-assemblyai ./livekit-plugins/livekit-plugins-assemblyai
COPY livekit-plugins/livekit-plugins-elevenlabs ./livekit-plugins/livekit-plugins-elevenlabs
COPY livekit-plugins/livekit-plugins-cartesia ./livekit-plugins/livekit-plugins-cartesia
COPY livekit-plugins/livekit-plugins-silero ./livekit-plugins/livekit-plugins-silero
COPY livekit-plugins/livekit-plugins-turn-detector ./livekit-plugins/livekit-plugins-turn-detector
COPY voice-platform/pyproject.toml voice-platform/uv.lock voice-platform/README.md ./voice-platform/
COPY voice-platform/src ./voice-platform/src
WORKDIR /src/voice-platform
RUN uv sync --frozen --no-dev --no-editable
# Download turn-detector / VAD model files at build time so pods start without network.
RUN mkdir -p /root/.cache/huggingface && (/src/voice-platform/.venv/bin/python -m voice_platform.apps.worker.main download-files || echo "model download skipped")

FROM python:3.12-slim AS runtime
RUN apt-get update && apt-get install -y --no-install-recommends libgomp1 ca-certificates \
    && rm -rf /var/lib/apt/lists/* && useradd --uid 10001 --create-home vp
WORKDIR /app
COPY --from=build /src/voice-platform/.venv /app/.venv
COPY --from=build /root/.cache/huggingface /home/vp/.cache/huggingface
COPY voice-platform/alembic.ini ./alembic.ini
COPY voice-platform/migrations ./migrations
COPY voice-platform/scenarios ./scenarios
RUN chown -R vp:vp /home/vp/.cache
ENV PATH="/app/.venv/bin:$PATH" PYTHONUNBUFFERED=1 VP_SCENARIO_LIBRARY_PATH=/app/scenarios \
    HF_HUB_OFFLINE=1
USER vp
EXPOSE 8000 9464
HEALTHCHECK --interval=15s --timeout=3s --start-period=20s CMD \
  python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz').status==200 else 1)" || exit 1
# Default: API. Override the command for: vp-worker start | vp jobs | vp migrate
CMD ["vp-api"]
