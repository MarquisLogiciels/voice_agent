# Voice Platform — Architecture

A multi-tenant platform for building, configuring, running, evaluating and improving AI
voice agents ("virtual employees"). It is built **on top of** LiveKit Agents, which lives
unmodified in the parent repository (`../livekit-agents`, `../livekit-plugins`). The
platform never patches the framework; it composes the framework's public interfaces.

The directory is self-contained (own `pyproject.toml`, `uv.lock`, tests, infra) so it can be
moved into its own repository by deleting the `[tool.uv.sources]` path overrides.

## 1. Bounded contexts and package map

```
voice-platform/
├── src/voice_platform/
│   ├── shared/         cross-cutting primitives: settings, logging+redaction, errors, crypto,
│   │                   ids, retry/backoff, circuit breaker, SSRF-safe HTTP, phone numbers
│   ├── core/           platform management: DB models, sessions+RLS context, tenants, users,
│   │                   RBAC, API keys, credentials vault, audit log, job queue
│   ├── config/         agent configuration: AgentConfig schema, validation, versioning,
│   │                   diffing, deployments, canary selection
│   ├── providers/      AI provider registry (STT/LLM/TTS/VAD/turn detection/realtime)
│   │                   → LiveKit plugin instances; fallback, health tracking, pricing/cost
│   ├── runtime/        live agent execution: session builder, PlatformAgent, conversation
│   │                   policies (silence, duration, context compaction), call recorder
│   ├── tools/          standardized agent tools + the guarded ToolExecutor
│   ├── integrations/   business-system adapters: HubSpot, Salesforce, Google Calendar,
│   │                   custom REST, Twilio messaging, in-memory sandbox adapters
│   ├── telephony/      LiveKit SIP provisioning, inbound routing, outbound dialing,
│   │                   blind/warm transfers, recording (egress), Twilio trunking
│   ├── knowledge/      document ingestion, extraction, chunking, embeddings, hybrid retrieval,
│   │                   retrieval evaluation
│   ├── evaluation/     scenarios, simulated callers, evaluators, benchmark runner
│   ├── optimization/   candidate generation, comparison, promotion gate, reports
│   ├── observability/  Prometheus metrics, OpenTelemetry span helpers and setup, statistics
│   └── apps/
│       ├── api/        FastAPI management API + webhooks + admin dashboard (static SPA)
│       ├── worker/     LiveKit AgentServer entrypoint (executes calls)
│       ├── jobs/       background job runner (ingestion, evaluations, optimizations)
│       └── cli.py      operational CLI (migrate, bootstrap, run evals locally)
├── migrations/         Alembic migrations (schema + row-level security)
├── scenarios/          evaluation scenario library (YAML)
├── tests/              unit / integration / e2e / load / evaluations
├── examples/           sample agent configurations
├── infrastructure/     docker, compose, terraform (AWS), monitoring (Prometheus/Grafana/OTel)
└── docs/               runbooks, provider & integration guides, security/privacy notes
```

Dependency direction (enforced in CI with `import-linter`, see `.importlinter`; higher layers
may import lower ones, never the reverse):

```
apps            (api, worker, jobs, cli — composition roots)
optimization
evaluation
runtime         (PlatformAgent, session factory, recorder, policies)
telephony       (SIP transport, transfers, outbound, provisioning, recording)
tools           (catalogue, executor, ledger, scheduling)
knowledge | integrations | providers
config          (AgentConfig, versioning service)
core            (models, db/RLS, auth, RBAC, vault, audit, jobs, retention)
observability   (metrics, span helpers; tracing_setup is used by apps only)
shared          (settings, logging, errors, crypto, resilience, http, blobstore, phone)
```

A second contract forbids `tools`, `integrations`, `config`, `core` and `knowledge` from
importing LiveKit at all, so business logic stays testable without the framework.

`runtime` is the only package that imports `livekit.agents.voice`. Business logic (tools,
integrations, config) is framework-agnostic and testable without audio.

## 2. Deployable processes

| Process | Command | Scales by | State |
|---|---|---|---|
| **api** | `vp-api` (uvicorn) | HTTP load, stateless | Postgres, Redis, object store |
| **agent-worker** | `vp-worker start` (LiveKit `AgentServer`) | concurrent calls; LiveKit load-balances jobs | Postgres (config read, transcript write) |
| **jobs** | `vp jobs` | queue depth | Postgres queue (`SKIP LOCKED`) |

External infrastructure: PostgreSQL 16 + pgvector, Redis, S3-compatible storage, LiveKit
server + SIP + Egress (LiveKit Cloud or self-hosted), a SIP trunk provider (Twilio, Telnyx…).

## 3. Live call data flow (existing LiveKit capabilities reused)

```
PSTN ─► SIP trunk (Twilio) ─► LiveKit SIP ─► dispatch rule ─► room + agent dispatch
                                                       │
                     vp-worker (AgentServer.rtc_session entrypoint)
                       1. resolve tenant/agent from dialed number or dispatch metadata
                       2. pick deployment version (canary hash) → PIN version for the call
                       3. build pipeline from ProviderRegistry (FallbackAdapters)
                       4. bind tools (integrations, transfer, knowledge) through ToolExecutor
                       5. AgentSession.start(PlatformAgent, room)
                       6. CallRecorder streams transcript/tool/metrics events to Postgres
                       7. egress recording to S3 (if enabled & consented)
                       8. on close: summary, cost, latency aggregates, end reason
```

Audio ingestion/output, VAD, STT streaming, end-of-turn detection, interruption/barge-in,
preemptive generation, tool calling, TTS streaming and playout are **LiveKit's**
(`AgentSession`, `AgentActivity`, `AudioRecognition`, `room_io`). The platform configures
them (`TurnHandlingOptions`, `user_away_timeout`, `max_tool_steps`, …) per agent version.

## 4. Provider system

`providers.registry.ProviderRegistry` maps `(kind, provider)` → `ProviderDescriptor`:
lazy import path, factory, capability flags, option schema, required secrets, pricing and an
optional health probe. Factories return **LiveKit objects** (`stt.STT`, `llm.LLM`, `tts.TTS`,
`vad.VAD`, turn detectors, `llm.RealtimeModel`) — no duplicate interfaces.

`providers.builder.PipelineBuilder` turns a validated `PipelineConfig` into components:

* validates capability compatibility (e.g. non-streaming STT ⇒ VAD required, `stt` turn
  detection ⇒ STT must emit end-of-speech, realtime mode ⇒ realtime model);
* wraps primary + fallbacks in LiveKit `FallbackAdapter`s with per-provider
  `APIConnectOptions` (timeout/retries);
* orders fallback chains using a process-wide `ProviderHealthTracker` (circuit breaker), so
  a provider that is failing for every call is skipped at session build time instead of
  being retried by each new call (prevents retry storms);
* resolves secrets from environment variables or per-tenant credentials (BYO keys).

Realtime (speech-to-speech) models are a separate `pipeline.mode: realtime`.

## 5. Tools and integrations

The LLM only sees **standardized tools** (`lookup_customer`, `book_appointment`,
`transfer_to_human`, …). Each tool declares an input schema, required capability and
whether it is consequential. `ToolExecutor` enforces, in order: schema validation → agent
permission → capability binding → confirmation policy → idempotency (Postgres-backed key) →
timeout → structured result → audit record. Integrations implement capability protocols
(`CRM`, `Calendar`, `Messaging`) and share `IntegrationHttpClient` (retry w/ jitter,
Retry-After, circuit breaker, error normalization, redaction, response-size limits). Custom
REST actions are declared in config and executed through an SSRF-guarded transport (host
allowlist, IP validation at connect time, no redirects, size limits).

## 6. Multi-tenancy and security

Every tenant-owned table has `tenant_id` and **FORCE ROW LEVEL SECURITY** with policies on
`current_setting('app.tenant_id')`. The API sets the tenant context per transaction from the
authenticated principal; repositories also filter explicitly (defense in depth). Cross-tenant
lookups that are legitimately needed (login, API-key auth, inbound number routing) go
through narrow `SECURITY DEFINER` SQL functions returning minimal columns. Credentials are
encrypted with an envelope key (`MultiFernet`, rotation supported) and are write-only through
the API. RBAC roles: `owner > admin > editor > viewer` per tenant, plus platform admins.

## 7. Configuration and versioning

`AgentConfig` (pydantic) is the single source of truth for an agent's behaviour. Versions are
immutable once published; drafts and optimizer candidates are separate versions with a
parent pointer. A deployment maps `(agent, environment)` → `version` (+ optional canary
version and percentage). The worker picks the version **once per call** (stable hash of the
call id) and records it on the conversation, so behaviour never changes mid-call. Rollback
re-points the deployment to a previous published version.

## 8. Evaluation and optimization

Scenarios (YAML) define persona, script or simulated goal, sandbox state (mock CRM/calendar
seed data), fault injection, and expectations. The runner drives the **real PlatformAgent and
ToolExecutor** through `AgentSession.run()` (text mode) against sandbox integrations — never
production systems — and optionally through an audio harness (TTS → noise/speed → STT).
Evaluators are deterministic first (tool calls, arguments, final sandbox state, false
success claims, repetition, latency) and LLM-judge second, using a judge model configured
independently from the agent's model. The optimizer creates candidate versions by applying
**allowlisted** config patches, re-runs the identical seeded suite, rejects regressions and
marks passing candidates as *eligible*; promotion to production requires a human approval.

## 9. Observability

Structured JSON logs carry `tenant_id`, `agent_id`, `agent_version`, `conversation_id`,
`call_id` via context variables; a redaction filter scrubs secrets, auth headers and
registered credential values. Prometheus metrics cover API, calls, per-provider latency
(STT, LLM TTFT, TTS TTFB, end-to-end), fallbacks, tool executions and integrations.
OpenTelemetry tracing reuses LiveKit's telemetry (`livekit.agents.telemetry`) with an OTLP
exporter. Cost per conversation is estimated from usage × a versioned pricing table.
