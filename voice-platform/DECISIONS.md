# Architecture Decision Records

Each record: context → decision → consequences. Newest last. Status is `accepted` unless noted.

## ADR-001 — Build the platform beside, not inside, the LiveKit framework
**Context.** The repository is a fork of `livekit/agents`. Modifying `livekit-agents` would
turn it into a hard-to-rebase fork (`voice/agent_activity.py` alone is ~5k lines).
**Decision.** All platform code lives in `voice-platform/`, a standalone uv project that
depends on `livekit-agents` and plugins. Local development resolves them from the parent
directories via `[tool.uv.sources]` path entries; extraction = delete those entries.
**Consequences.** Zero framework diffs so far. Any future framework change must be proposed
upstream or isolated in `voice_platform/runtime/_compat.py` with a justification.

## ADR-002 — One Python distribution with enforced internal boundaries
**Context.** The suggested layout lists ~15 packages. Separate distributions add release and
dependency overhead without a scaling benefit at this stage.
**Decision.** A single `voice_platform` distribution with sub-packages per bounded context and
three process entry points (`api`, `worker`, `jobs`). Boundaries are enforced with
`import-linter` contracts in CI.
**Consequences.** Any context can be split into its own service later because cross-context
calls already go through explicit interfaces.

## ADR-003 — Reuse LiveKit interfaces as the provider abstraction
**Decision.** Provider factories return LiveKit `stt.STT`, `llm.LLM`, `tts.TTS`, `vad.VAD`,
turn detectors and `llm.RealtimeModel`. The registry adds metadata (capabilities, option
schema, secrets, pricing, health) but no wrapper interface. Fallback uses LiveKit's
`FallbackAdapter`s; non-streaming adaptation uses LiveKit's `StreamAdapter`s.
**Consequences.** Every LiveKit plugin is usable by registering a descriptor (~15 lines).

## ADR-004 — Process-wide provider circuit breaker on top of per-session fallback
**Context.** LiveKit `FallbackAdapter` state is per instance. Sessions are built per call, so a
dead primary would be retried by every new call (retry storm, added latency).
**Decision.** `ProviderHealthTracker` records failures/successes from adapter events across
sessions in a worker process; the builder moves open-circuit providers to the end of the
chain. Half-open probing happens naturally when the cooldown expires.

## ADR-005 — PostgreSQL row-level security for tenant isolation
**Decision.** Every tenant table has `tenant_id`, RLS **forced** (applies to the table owner),
policy on `current_setting('app.tenant_id', true)`. The application sets it with
`set_config(..., true)` (transaction-local) from the authenticated principal. Platform-admin
operations set `app.platform_admin='on'`. Cross-tenant lookups (login, API key, inbound
routing) use narrow `SECURITY DEFINER` functions. Repositories still filter by tenant.
**Consequences.** A missing `WHERE tenant_id` cannot leak data; tests assert isolation with raw
SQL. The DB role used by the app must not be a superuser (superusers bypass RLS).

## ADR-006 — Postgres-backed job queue instead of a broker
**Decision.** Background work (ingestion, evaluations, optimizations) uses a `jobs` table with
`FOR UPDATE SKIP LOCKED`, attempts, backoff and visibility timeouts.
**Consequences.** No extra infrastructure; throughput is ample for these job types. Move to a
broker only if job volume requires it.

## ADR-007 — pgvector + Postgres full-text search for RAG (hybrid, RRF)
**Decision.** Chunks are stored in Postgres with a `vector(768)` embedding and a generated
`tsvector`. Retrieval fuses vector and keyword rankings with reciprocal-rank fusion. Embeddings
come from a provider in the registry (default OpenAI `text-embedding-3-small` at 768 dims); an
offline lexical-hash embedder exists for development and CI and is labelled as such.
**Consequences.** Tenant isolation comes from the same RLS policies; no separate vector DB.

## ADR-008 — Envelope-encrypted credential vault
**Decision.** Credentials are JSON payloads encrypted with `cryptography` `MultiFernet` using
keys from `VP_ENCRYPTION_KEYS` (first key encrypts, all decrypt → rotation). Payloads are never
returned by the API; only metadata. Decrypted values are registered with the log redactor.
**Consequences.** A KMS-backed key provider can replace the env key without schema changes.

## ADR-009 — Pin configuration per call, canary by stable hash
**Decision.** At call start the worker resolves the deployment and picks the canary version if
`sha256(call_id) mod 10000 < canary_percent*100`. The chosen version id is stored on the
conversation and the config object is held in memory for the whole session.

## ADR-010 — Standardized tools; integrations behind capability protocols
**Decision.** The LLM sees a fixed catalogue of tools whose schemas the platform owns. Tenants
bind tools to integrations (`crm: hubspot_main`). Custom REST actions are declared in config,
never constructed by the LLM. Consequential tools require an explicit caller-confirmation flag
and server-side precondition checks (e.g. slot availability), are idempotent per
`(conversation, tool, canonical args)` and are audited.

## ADR-011 — Human transfer via LiveKit SIP REFER (blind) and LiveKit `WarmTransferTask`
**Decision.** Blind transfers use `SipService.transfer_sip_participant` (SIP REFER). Attended
transfers reuse `livekit.agents.beta.workflows.WarmTransferTask` (consult room, briefing,
merge via `move_participant`). Destinations are resolved from config rules with fallback
chains; SIP status codes are mapped to busy / no-answer / unavailable / rejected.
**Consequences.** Blind transfer requires a trunk that accepts REFER (Twilio: enable *PSTN
Transfer*). Limitations documented in `docs/telephony.md`.

## ADR-012 — Evaluation drives the real agent in text mode; audio harness optional
**Decision.** Scenarios run `AgentSession.run()` with the real `PlatformAgent`, tools and
executor against sandbox integrations. An audio harness (TTS → perturbation → STT) exercises
the speech path when provider keys exist. Deterministic checks are primary; the LLM judge
must use a model configured separately from the agent under test.

## ADR-013 — Optimizer may only patch allowlisted config paths; humans promote
**Decision.** Candidates are produced from JSON patches restricted to an allowlist
(instructions, persona, tool descriptions, model/provider choices from an approved list,
turn-handling within bounds, retrieval settings). Promotion requires no critical regression,
configurable thresholds and an admin approval. The optimizer cannot touch code or infra.

## ADR-014 — Dashboard as a dependency-free static SPA served by the API
**Context.** A Node build pipeline adds a second toolchain to deploy and secure.
**Decision.** The admin dashboard is plain ES modules + a small CSS design system served by the
API at `/dashboard`, talking only to the public REST API with the user's JWT. Playwright
smoke tests cover the main flows.
**Consequences.** Simple deploys; if the UI grows substantially it can move to a framework
without API changes.

## ADR-015 — stdlib logging with JSON formatter and redaction (no structlog)
**Decision.** LiveKit uses stdlib `logging`; the platform adds a JSON formatter, contextvar
enrichment and a redaction filter on the root logger so framework logs get the same treatment.

## ADR-016 — httpx for integrations
**Decision.** Integrations use `httpx.AsyncClient` (connection pooling, timeouts, `respx` for
tests). The SSRF guard is implemented as a custom httpcore network backend that validates the
resolved IP at connect time (defeats DNS rebinding between check and connect).

## ADR-017 — Optimizer accepts partial improvements between iterations
**Context.** Greedy hill-climbing on pass rate rejected each of two complementary fixes that
only pass scenarios together (observed in tests).
**Decision.** Between iterations a candidate with *no regressions* and equal pass rate is kept
if its mean score improves by ≥ 0.01; the final gate against the original baseline still
requires a strictly higher pass rate, the minimum pass rate and no critical regressions.

## ADR-018 — Tool side effects fail closed without an idempotency ledger
**Decision.** If the ledger cannot reserve a key (database down), a deduplicated
(consequential) tool returns `unavailable` instead of executing. Duplicate external writes are
worse than a deferred action the agent can hand to a human.

## ADR-019 — Provider failures counted only when final
**Decision.** The circuit breaker counts `error` events with `recoverable=false` and
FallbackAdapter availability changes; errors LiveKit is still retrying don't open circuits.
