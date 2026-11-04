# Implementation Plan

Status legend: `[x]` implemented and tested locally · `[~]` implemented, partially verified
(see notes) · `[ ]` not started. "Locally tested" means automated tests in this repo against
real Postgres/Redis and mocked external HTTP/LiveKit APIs. Nothing here is production-verified
until it has run against real provider accounts (see PRODUCTION_READINESS.md).

## Milestones and acceptance criteria

| # | Milestone | Acceptance criteria |
|---|---|---|
| M1 | Foundations | migrations apply on clean PG16; RLS isolation tests pass; auth + RBAC enforced on every route; JSON logs redact secrets |
| M2 | Config & versioning | publish/rollback/diff/canary tested; worker pins version per call |
| M3 | Provider system | every registered provider instantiates from config without network; fallback + circuit breaker tested with failing fakes |
| M4 | Runtime | text-mode session runs end-to-end with tools; transcripts/metrics persisted; silence + max-duration policies tested |
| M5 | Telephony | inbound routing, outbound dial, blind + warm transfer, failure mapping and fallbacks tested against mocked LiveKit API |
| M6 | Integrations | HubSpot/Salesforce/GCal/custom REST adapters tested with recorded-shape HTTP mocks; SSRF guard tested; idempotency tested |
| M7 | Knowledge | ingest PDF/DOCX/TXT/HTML, hybrid retrieval with tenant isolation, retrieval eval metrics |
| M8 | Evaluation & optimization | scenario suite runs deterministically; optimizer produces candidate, rejects regressions, requires approval |
| M9 | Dashboard & API | each UI action hits a real endpoint; Playwright smoke test |
| M10 | Hardening | Docker images build, compose stack documented, CI workflow, load test numbers recorded, runbooks |

## Checklist

The live checklist is maintained at the bottom of this file and updated at the end of every
working session.

## Risks

1. **No provider credentials in the build environment** — provider factories, integrations and
   telephony are verified with mocks only; live verification steps are listed per component.
2. **LiveKit server/SIP not runnable here** (no Docker daemon, GitHub releases blocked) — the
   worker's call handler is tested with a stand-in JobContext (routing, dial failures) and the
   agent itself through LiveKit text sessions, not real rooms or audio.
3. **Evaluation quality depends on real LLMs** — optimization loop logic is tested with
   deterministic LLM doubles; real improvement numbers require API keys.
4. **Blind transfer depends on trunk REFER support** — documented per provider.

## Live checklist
(updated at the end of each session — see bottom of file)

### Session 1 status (end of session)

**Foundations**
- [x] Standalone uv project beside the framework (no LiveKit changes) — ADR-001
- [x] Settings with production invariants; JSON logging with context + secret redaction
- [x] PostgreSQL schema, Alembic migrations (round-trip tested), forced RLS on all tenant tables
- [x] Auth (argon2 + JWT, API keys), RBAC, append-only audit log, rate limiting (Redis/local)
- [x] Credential vault (MultiFernet, rotation), Postgres job queue with leases
- [x] Retention sweep job

**Configuration & versioning**
- [x] AgentConfig schema with cross-field validation and environment overrides
- [x] Drafts/candidates/published versions, diff, deploy, canary (stable hash), promote, rollback
- [x] Per-call version pinning

**Providers & runtime**
- [x] Provider registry (20 descriptors), option schemas, BYO keys, pricing table, health probes
- [x] Pipeline builder → LiveKit FallbackAdapters; process-wide circuit breaker
- [x] PlatformAgent on AgentSession; prompt builder; race-safe context compaction
- [x] Silence and max-duration policies; per-turn latency/usage recorder; cost estimates
- [x] Worker (AgentServer) + CallHandler: inbound routing, outbound dial, recording, transfers
- [ ] Audio-level simulation harness (TTS → noise/speed → STT) — designed, not built
- [ ] Live verification with LiveKit + providers (needs credentials)

**Telephony**
- [x] LiveKit SIP transport (REFER transfer, dial, hangup) with SIP status mapping
- [x] Transfer manager (rules, business hours, fallback chains, blind + warm, announcements)
- [x] Outbound guardrails (allowlist, DNC, calling window, dry run) and dispatch
- [x] SIP provisioning (inbound trunk + dispatch rule, outbound trunk), Twilio trunking + SMS
- [ ] Live call tests (inbound/outbound/transfers/recording)

**Integrations & tools**
- [x] HubSpot, Salesforce, Google Calendar, custom REST (SSRF-guarded), Twilio, sandbox
- [x] Standard tool catalogue (14 tools) + guarded executor with Postgres idempotency ledger
- [ ] Sandbox-account verification for HubSpot/Salesforce/Google/Twilio

**Knowledge**
- [x] PDF/DOCX/HTML/text/URL ingestion, chunking, OpenAI + offline embeddings, hybrid retrieval
- [x] Accent-insensitive English/French stemmed full-text search; retrieval evaluation metrics

**Evaluation & optimization**
- [x] Scenario schema + 10-scenario receptionist library; scripted + LLM callers
- [x] Deterministic evaluators + independent LLM judge; seeded runner with Wilson CIs
- [x] Optimizer: allowlisted patches, heuristic + LLM proposers, regression-rejecting gate,
      candidate versions, human approval → canary
- [x] LLM scenario generation endpoint (validated, sandbox-forced, tagged `generated`)
- [ ] Benchmark with real models (needs keys)

**Management**
- [x] REST API for all resources; admin dashboard (Playwright smoke test)
- [x] Data-subject export and erasure by phone number

**Hardening**
- [x] Prometheus metrics, OTel tracing (PII-stripped), Grafana dashboard, alert rules
- [x] Dockerfile, compose stack, AWS Terraform, GitHub Actions workflow
- [x] ruff, mypy --strict, import-linter contracts all clean; 115 tests
- [x] Load measurements recorded (platform overhead; API throughput)
- [ ] Docker build / compose up / terraform validate / CI run (unavailable in this environment)

### Next concrete steps
1. Provide staging credentials (LiveKit, Twilio, one STT/LLM/TTS set) and run the go-live
   checklist in PRODUCTION_READINESS.md; fix whatever live calls reveal.
2. Run the receptionist suite against real models with ≥3 repetitions; tune the library.
3. Build the audio simulation harness (custom `AudioInput`/`AudioOutput` around AgentSession).
4. Review workflow for generated scenarios (approve before they gate promotion).
5. SSO/MFA; pip-audit in CI; KMS-backed vault keys.

### Session 2 status (validation & hardening)

Evidence for every item: VALIDATION_REPORT.md. Labels as in PRODUCTION_READINESS.md.

**Live validation (self-hosted lab: LiveKit server + SIP, Ollama/Qwen 2.5 3B, Vosk, Pico)**
- [x] Live WebRTC call through `vp-worker start` (4/4 runs) — tested against a real sandbox
- [x] SIP: inbound, outbound answered/busy, blind transfer, busy→fallback, no answer, caller
      hang-up during transfer (7/7) — tested against a real sandbox
- [x] Audio harness: WER by voice/rate/noise/telephone channel (EN/FR), VAD boundaries,
      flush latency, false triggers
- [x] Real-model benchmark + optimizer run (see VALIDATION_REPORT §3.9)
- [x] LLM-initiated transfer announcement (F5) — verified with a deterministic model
- [ ] Warm transfer, recording/egress, DTMF; SIP lab re-run with the T6 mitigation
- [ ] Real carrier + vendor sandboxes (docs/telephony-staging.md checklist)

**Defects fixed this session (found by running, not by reading)**: worker could never start a
job (L1); NO_PROXY ignored by LiveKit (L2); Vosk ghost words/latency (L3, L4); hours read as
"nine minus seventeen" (L5); narrated-but-never-made tool calls (L6); wrong dates from the
model (L7); cold first turn (L8); `transfer_on_request` dead config (T1); transfer tool missing
(T2); transfers cut by the 8 s tool timeout (T3); inbound greeting on outbound calls (T4);
docs never committed (root `.gitignore`); transcripts and phone numbers in worker logs;
erased recordings recoverable from S3 versions; SSE header bypassing the KMS key; ECS killing
calls 120 s into a deployment.

**Audit fixes**: F1 consent ordering, F2 booking race, F3 IntegrityError→409, F4 bounded
extraction, F5 announcement inside the transfer tool (verified in the SIP lab), F6 turn
detector registered in the worker main process (unit tested; real model files not verified),
F7 http for trusted endpoints. Benchmark-driven: B1 escape-hatch negation, B2 follow-through
coverage + no spoken tool-call text, B3 document prompt-injection sanitiser; T6 LiveKit REFER
race mitigated (unit tested).

**Infrastructure**: terraform fmt/validate + checkov in CI, VPC module pinned to a commit,
tightened SGs, compose health checks; pip-audit / dependency review / Trivy in CI.

### Next concrete steps
1. Staging: carrier trunk + LiveKit (Cloud or self-hosted with public IP) + one commercial
   STT/LLM/TTS set; run docs/telephony-staging.md and the benchmark with a production-grade
   model and an independent judge.
2. Build and scan the image in CI; `terraform plan` in a staging account.
3. PgBouncer/RDS Proxy or smaller per-job DB pools; concurrent-call load test; set `load_fnc`.
4. Exercise warm transfer, recording egress, DTMF; re-run the SIP lab with the T6 mitigation.
5. Dashboard SSO/MFA via the customer IdP; per-session revocation; KMS-backed vault keys.
6. HubSpot portals: create the idempotency property; Salesforce: External ID field.
