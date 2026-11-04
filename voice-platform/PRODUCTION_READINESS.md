# Production Readiness

**Status: not production-verified.** Everything below was implemented and tested in a
sandboxed build environment **without provider credentials, without a LiveKit server and
without a SIP trunk**. "Locally tested" = automated tests against real PostgreSQL 16 + pgvector,
real Redis semantics (fakeredis) and HTTP/LiveKit API mocks shaped like the documented APIs.

## Verification matrix

| Capability | Implemented | Locally tested | Sandbox tested | Production verified |
|---|---|---|---|---|
| Tenant isolation (RLS), RBAC, API keys, audit log | ✅ | ✅ raw-SQL isolation tests, API tests | – | ❌ |
| Credential vault (encryption, rotation, write-only) | ✅ | ✅ | – | ❌ |
| Agent config schema, versions, diff, deploy, canary, rollback, per-call pinning | ✅ | ✅ | – | ❌ |
| Provider registry + builder (Deepgram, AssemblyAI, OpenAI, Google, Cartesia, ElevenLabs, Anthropic, LiveKit Inference, Silero, turn detector, OpenAI/Gemini realtime) | ✅ | ✅ instantiation + LiveKit FallbackAdapter switching with fake models | ❌ needs API keys | ❌ |
| Cross-call provider circuit breaker | ✅ | ✅ | ❌ | ❌ |
| Conversation runtime on LiveKit AgentSession (text mode) incl. tools, transfer, persistence | ✅ | ✅ e2e | – | ❌ |
| Audio path (STT→LLM→TTS over LiveKit rooms) | ✅ (LiveKit's) | ❌ no LiveKit server here | ❌ | ❌ |
| Inbound SIP routing, outbound dialing | ✅ | ✅ DB routing; dial failure path with fake JobContext | ❌ needs LiveKit SIP + trunk | ❌ |
| Blind transfer (SIP REFER) with fallback chain | ✅ | ✅ mocked transport, SIP status mapping | ❌ | ❌ |
| Warm transfer (LiveKit `WarmTransferTask`) | ✅ wired | ❌ needs live SIP | ❌ | ❌ |
| Recording via egress + consent announcement | ✅ | ❌ needs egress | ❌ | ❌ |
| HubSpot / Salesforce / Google Calendar adapters | ✅ | ✅ against documented API shapes | ❌ needs sandbox accounts | ❌ |
| Custom REST actions with SSRF protection | ✅ | ✅ (DNS-rebinding-safe connect guard, path encoding, allowlist) | – | ❌ |
| Twilio SMS + webhook signature validation + trunk provisioning | ✅ | ✅ signature; SMS/trunk via mocks | ❌ | ❌ |
| Tool executor (validation, confirmation, idempotency, timeouts, audit) | ✅ | ✅ incl. concurrent duplicates + worker restart | – | ❌ |
| Knowledge ingestion (PDF/DOCX/HTML/text/URL) + hybrid retrieval | ✅ | ✅ with offline lexical embedder | ❌ OpenAI embeddings need key | ❌ |
| Evaluation engine, scenario library, LLM judge | ✅ | ✅ deterministic machinery with scripted model | ❌ real-model runs need keys | ❌ |
| Optimizer + promotion gate + human approval | ✅ | ✅ (improves instruction-sensitive scripted model; rejects regressions) | ❌ | ❌ |
| Admin dashboard | ✅ | ✅ Playwright smoke test against live API | – | ❌ |
| Retention sweep | ✅ | ✅ | – | ❌ |
| Observability: JSON logs w/ redaction, Prometheus, OTel tracing, Grafana dashboard, alerts | ✅ | ✅ redaction + metrics endpoint | ❌ | ❌ |
| Docker image, compose stack | ✅ | ❌ no Docker daemon here | ❌ | ❌ |
| Terraform (AWS) | ✅ | ⚠️ `terraform fmt` only (registry blocked → `validate` not run) | ❌ | ❌ |
| CI workflow | ✅ | ❌ not executed on GitHub yet | – | – |

## Performance targets (measured vs. not)

Measured on this build machine (4 vCPU), `tests/load` → `docs/load-test-results.json`:

| Measurement | Result | Interpretation |
|---|---|---|
| Platform overhead per turn, 10 concurrent text conversations in **one** process (simulated 250 ms TTFT ×2 per turn) | p95 ≈ 54 ms, event-loop lag p95 ≈ 3 ms | small vs. provider latency |
| Same with 50 / 100 conversations in one process | overhead p95 ≈ 0.34 s / 0.58 s; loop lag p95 ≈ 62 ms / 171 ms | one event loop should not host many calls — LiveKit runs each job in its own process, which is how the worker is deployed |
| API `GET /agents` (JWT + 2 RLS transactions), in-process, 1 / 10 / 50 in flight | p50 ≈ 6 / 39 / 176 ms; ~160–240 req/s per process | scale with replicas/uvicorn workers; per-request auth lookups could be cached if needed |

**Not measured:** end-to-end voice latency (needs real STT/LLM/TTS + LiveKit), the ~1 s
response target, STT accuracy, TTS quality, interruption latency, concurrent calls per worker
host. The runtime records eou + LLM TTFT + TTS TTFB per turn and exposes p50/p95 per agent
(`vp_response_latency_seconds`) so these can be measured the moment real calls run.

## Security checklist

- [x] RLS forced on every tenant table; app role is non-superuser, no BYPASSRLS
- [x] Cross-tenant lookups only via narrow functions (login, API key, inbound routing)
- [x] Credentials encrypted (Fernet/MultiFernet), write-only API, rotation command
- [x] Secrets redacted from logs (patterns + registered decrypted values); PII-free traces by default
- [x] SSRF: scheme/port/host allowlists, connect-time IP validation, no redirects, size limits
- [x] Tools: fixed schemas, confirmation for consequential actions, server-side precondition
      checks, idempotency, per-call limits, caller-only appointment/SMS scope
- [x] Prompt-injection hardening: platform rules appended after tenant text; tool output
      declared as data; LLM cannot choose URLs, headers or credentials
- [x] Webhooks: Twilio HMAC signature, LiveKit JWT verification
- [x] Rate limiting (Redis with local fallback), login throttling, strict CSP on dashboard
- [x] Append-only audit log (trigger + revoked grants)
- [x] Retention enforcement job; recording consent announcement before egress starts
- [ ] Dependency vulnerability scan in CI (add `pip-audit` / GitHub Dependabot)
- [ ] External penetration test
- [ ] KMS-backed key provider for the vault (env keys today)
- [ ] SSO / MFA for dashboard users (password + JWT today)

## Go-live checklist (per environment)

1. Provision infra (Terraform), set Secrets Manager values, run `vp migrate` as the owner role,
   confirm the runtime role cannot `SET row_security = off` and is not superuser.
2. LiveKit Cloud (or self-hosted server + SIP + egress), Twilio Elastic SIP trunk with
   *PSTN Transfer* enabled (needed for blind REFER transfers), origination URI → LiveKit SIP.
3. Run `GET /v1/providers/health?probe=true` (platform admin) — all configured providers green.
4. Per tenant: create credentials, integrations → `POST …/integrations/{id}/test` green.
5. Run the scenario suite against the agent with real providers (`vp eval` or the
   Evaluations page), with a judge model different from the agent's model; review failures.
6. Place test calls: inbound, outbound (allowlisted test number), blind + warm transfer, busy
   destination fallback, recording on/off, silence timeout, max duration.
7. Verify dashboards/alerts receive data; trigger a provider failure and confirm fallback +
   `ProviderFailing` alert.
8. Backups: confirm RDS PITR and a restore drill (docs/operations.md).
9. Legal/privacy review (docs/security-privacy.md) before recording or calling customers.

## Known limitations

* Text-mode evaluation exercises the agent's reasoning and tools, not ASR/TTS quality; the
  audio perturbation harness (TTS → noise/speed → STT) is designed but not implemented.
* The hashing embedder is lexical; production tenants should use semantic embeddings.
* Salesforce lead creation is idempotent only when an External ID field is configured;
  HubSpot has no idempotency keys (dedupe-before-create + executor ledger mitigate).
* Twilio Messages API has no idempotency key; ambiguous send failures are not retried.
* Optimizer improvements were demonstrated on an instruction-sensitive scripted model only;
  real-model gains are unmeasured.
