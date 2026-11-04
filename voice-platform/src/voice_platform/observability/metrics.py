"""Prometheus metrics (single registry, defined once).

Label cardinality is bounded on purpose: tenant ids are NOT used as labels on high-volume
series (per-tenant analytics come from Postgres); provider/model/tool/integration names are
bounded by the catalogue.
"""

from __future__ import annotations

from prometheus_client import Counter, Gauge, Histogram

LATENCY_BUCKETS = (0.05, 0.1, 0.2, 0.3, 0.5, 0.75, 1.0, 1.5, 2.0, 3.0, 5.0, 8.0, 13.0)

# --- API ---------------------------------------------------------------------------------------
API_REQUESTS = Counter("vp_api_requests_total", "API requests", ["method", "route", "status"])
API_LATENCY = Histogram(
    "vp_api_request_seconds", "API latency", ["method", "route"], buckets=LATENCY_BUCKETS
)

# --- calls -------------------------------------------------------------------------------------
ACTIVE_CALLS = Gauge("vp_active_calls", "Calls currently handled by this worker")
CALLS_TOTAL = Counter("vp_calls_total", "Calls handled", ["direction", "end_reason"])
CALL_DURATION = Histogram(
    "vp_call_duration_seconds",
    "Call duration",
    buckets=(10, 30, 60, 120, 300, 600, 1200, 1800, 3600),
)
CALL_SETUP = Histogram(
    "vp_call_setup_seconds", "Job start → session started", buckets=LATENCY_BUCKETS
)

# --- per-provider latency ----------------------------------------------------------------------
STT_DELAY = Histogram(
    "vp_stt_transcription_delay_seconds",
    "End of speech → final transcript",
    ["provider"],
    buckets=LATENCY_BUCKETS,
)
EOU_DELAY = Histogram(
    "vp_end_of_utterance_delay_seconds",
    "End of speech → turn committed",
    ["provider"],
    buckets=LATENCY_BUCKETS,
)
LLM_TTFT = Histogram(
    "vp_llm_ttft_seconds", "LLM time to first token", ["provider", "model"], buckets=LATENCY_BUCKETS
)
TTS_TTFB = Histogram(
    "vp_tts_ttfb_seconds",
    "TTS time to first audio byte",
    ["provider", "model"],
    buckets=LATENCY_BUCKETS,
)
E2E_LATENCY = Histogram(
    "vp_response_latency_seconds",
    "User end of speech → first agent audio (eou + ttft + ttfb)",
    ["agent"],
    buckets=LATENCY_BUCKETS,
)
PROVIDER_ERRORS = Counter(
    "vp_provider_errors_total", "Provider errors", ["kind", "provider", "recoverable"]
)
PROVIDER_FALLBACKS = Counter(
    "vp_provider_fallbacks_total", "Fallback activations", ["kind", "provider"]
)
LLM_TOKENS = Counter("vp_llm_tokens_total", "LLM tokens", ["provider", "model", "type"])
TTS_CHARS = Counter("vp_tts_characters_total", "TTS characters", ["provider"])
STT_AUDIO = Counter("vp_stt_audio_seconds_total", "Audio seconds sent to STT", ["provider"])

# --- tools & integrations ----------------------------------------------------------------------
TOOL_CALLS = Counter("vp_tool_calls_total", "Tool executions", ["tool", "status"])
TOOL_LATENCY = Histogram(
    "vp_tool_seconds", "Tool execution time", ["tool"], buckets=LATENCY_BUCKETS
)
INTEGRATION_REQUESTS = Counter(
    "vp_integration_requests_total",
    "Integration HTTP requests",
    ["integration", "operation", "status"],
)
INTEGRATION_LATENCY = Histogram(
    "vp_integration_request_seconds",
    "Integration request latency",
    ["integration", "operation"],
    buckets=LATENCY_BUCKETS,
)

# --- telephony ---------------------------------------------------------------------------------
TRANSFERS = Counter("vp_transfers_total", "Human transfers", ["mode", "outcome"])
OUTBOUND_DIALS = Counter("vp_outbound_dials_total", "Outbound dials", ["outcome"])

# --- jobs --------------------------------------------------------------------------------------
JOBS = Counter("vp_jobs_total", "Background jobs processed", ["kind", "status"])
JOB_DURATION = Histogram(
    "vp_job_seconds",
    "Background job duration",
    ["kind"],
    buckets=(0.1, 0.5, 1, 5, 15, 60, 300, 900, 3600),
)
