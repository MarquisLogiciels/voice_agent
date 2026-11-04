# Production Readiness

**Status: not production-verified.** No real carrier, vendor account or production LiveKit
deployment has been used. Since mission 2 the platform *has* run complete voice calls through a
real LiveKit server and LiveKit SIP with real (self-hosted) speech and language models; see
[VALIDATION_REPORT.md](VALIDATION_REPORT.md) for every command, number and defect behind the
labels below.

## Status labels

| Label | Meaning |
|---|---|
| **Implemented** | code exists; no automated test exercises it |
| **Unit tested** | isolated tests with in-process doubles |
| **Integration tested with mocks** | real Postgres/framework, external vendors mocked |
| **Tested against a real sandbox** | a real external service or real self-hosted equivalent (LiveKit server/SIP, Ollama, Vosk) — not the vendor's production cloud |
| **Verified in a real end-to-end environment** | real carrier + real vendor accounts |
| **Not verified** | not exercised |

Nothing is in the fifth category yet.

## Capability matrix

| Capability | Status | Evidence / caveat |
|---|---|---|
| Tenant isolation (forced RLS), RBAC, API keys, audit log | Integration tested with mocks | raw-SQL isolation tests; races map to 409 (F3) |
| Credential vault, rotation, write-only API | Integration tested with mocks | |
| Agent config, versions, deploy, canary, rollback, per-call pinning | Integration tested with mocks | |
| Live voice pipeline: WebRTC audio → VAD → STT → turn end → LLM → tools → TTS → audio | **Tested against a real sandbox** | LiveKit 1.13 server, Vosk, Qwen 2.5 3B, Pico TTS; 4/4 repeated runs; barge-in stops agent in 0.65–0.69 s |
| Production worker process model (`vp-worker start`, process per job) | **Tested against a real sandbox** | found and fixed L1 (no job could ever start) |
| Inbound PSTN-style call via SIP | **Tested against a real sandbox** | LiveKit SIP v1.23 + scripted SIP endpoint; INVITE→200 in 0.97 s; G.711 greeting WER 0.00 |
| Outbound call via API guardrails → SIP (answered / busy) | **Tested against a real sandbox** | allowlist + calling window; `completed` / `busy` |
| Blind transfer (REFER), busy → fallback, no answer, caller hang-up mid-transfer | **Tested against a real sandbox** | REFER/NOTIFY observed; 7/7 SIP scenarios pass |
| Caller asks for a person → transfer (deterministic) | Tested against a real sandbox | `transfer_on_request` was dead config before (T1) |
| Warm transfer (`WarmTransferTask`) | Implemented | **Not verified** |
| Transfer announcement from an LLM-initiated tool call (F5) | Implemented | **Not verified** |
| Recording via egress + consent ordering | Unit tested | consent now plays before egress starts (F1); egress itself **not verified** |
| DTMF / IVR detection | Implemented | **Not verified** |
| Self-hosted providers: Vosk STT (flush, noise gate, ghost-word filter), Pico/eSpeak TTS | Tested against a real sandbox | audio harness; not production-quality voices |
| Commercial providers (Deepgram, OpenAI, Cartesia, ElevenLabs, Anthropic, Google, AssemblyAI, LiveKit Inference) | Unit tested | instantiation + fallback with fakes; **not verified** with real keys |
| Provider endpoint trust (no platform keys to tenant URLs) | Unit tested | security fix in mission 2 |
| HubSpot / Google Calendar request contracts | Integration tested with mocks | **machine-checked against the vendors' published specs** (HubSpot SDK models, Calendar v3 discovery document) |
| Salesforce adapter | Integration tested with mocks | reviewed against Salesforce docs; no machine-readable spec |
| Business workflow with lost responses, retries, restart | Integration tested with mocks | real adapters + stateful vendor fakes + real Postgres ledger; HubSpot needs `idempotency_property` to avoid duplicates |
| Booking race between concurrent calls (F2) | Unit tested | post-insert verification; residual risk if the calendar list is not read-after-write |
| Knowledge ingestion + hybrid retrieval | Integration tested with mocks | extraction bounded (F4); semantic embeddings **not verified** |
| Evaluation engine + scenario libraries | **Tested against a real sandbox** | real-model benchmark (Qwen 2.5 3B) — see below |
| Optimizer + promotion gate | **Tested against a real sandbox** | real-model run — see below; human approval enforced by API |
| Data-subject export/erasure incl. all recording versions, re-application after restore | Integration tested with mocks | moto (versioned S3) + Postgres; **restore drill not performed** |
| PII out of logs (LiveKit `lk.pii.*`, phone numbers in room names) | Unit tested | lab logs showed caller numbers before the fix |
| Admin dashboard | Integration tested with mocks | Playwright smoke test |
| Terraform (AWS) | `fmt` + `validate` pass; checkov 132 pass / 4 known gaps | `plan`/`apply` **not verified** (no AWS account) |
| Docker image | Implemented | **not built** in this environment after the daemon became unavailable |
| Compose stacks | `docker compose config` valid | lab stack ran; app stack not started |
| CI (quality, tests, pip-audit, dependency review, terraform, checkov, image + Trivy) | Implemented | not executed on GitHub yet; pip-audit run locally: 0 known vulnerabilities |
| ECS deployment safety (task protection during calls, worker health check) | Unit tested | **not verified** on ECS |

## Measured performance (lab, CPU-only, 4 vCPU)

| Measurement | Result | What it means |
|---|---|---|
| Caller-perceived response, live call (end of caller audio → agent audio) | 2.3–4.5 s | dominated by CPU LLM inference; EOU p50 1.0 s, TTS TTFB 0.03 s |
| First turn with prompt prewarm | ~3.8 s (12–27 s without) | `conversation.prewarm_llm_prompt` for prefix-caching self-hosted servers |
| Barge-in → agent silent | 0.65–0.69 s | Silero VAD, `min_duration_s=0.4` |
| VAD end-of-speech detection | p50 0.69 s after true end | includes Silero's min silence |
| STT flush → final transcript | p50 0.49 s | Vosk with manual flush (was ~1.9 s) |
| Vosk WER, clean synthetic speech EN / FR | 0.9 % / 0.0 % | synthetic voices; not real callers |
| Vosk WER, 8 kHz telephone channel EN / FR | 18 % / 4 % | the wideband EN model degrades on phone audio — use a telephony STT |
| Vosk WER at 10 dB / 5 dB SNR (EN) | 17 % / 80 % | |

**Not measured:** latency and accuracy with commercial providers, concurrent calls per worker
host (the box is saturated by the local LLM), real caller audio, real carrier behaviour.
Capacity guidance and the DB-connection fan-out risk are in VALIDATION_REPORT §3.8.

## Security checklist

- [x] RLS forced on every tenant table; app role non-superuser, no BYPASSRLS
- [x] Credentials encrypted, write-only API, rotation; platform provider keys never sent to tenant-chosen endpoints
- [x] SSRF protection for integrations, URL ingestion and OpenAI-compatible endpoints
- [x] Tools: fixed schemas, confirmation for consequential actions, server-side checks, idempotency ledger, per-tool time budgets
- [x] Prompt injection: platform rules after tenant text, tool output as data; benchmarked (`injection-caller`, `injection-in-document`)
- [x] Bounded document extraction (process isolation, timeouts, zip-bomb limits)
- [x] PII kept out of logs (incl. LiveKit's handlers); recordings purged with all versions; erasure re-applicable after restore
- [x] Dependency (pip-audit), dependency-review and image (Trivy) scanning in CI; IaC scanning (checkov)
- [ ] SSO/MFA for dashboard users — decision: delegate to the customer IdP / identity-aware proxy (docs/security-privacy.md)
- [ ] Per-session token revocation
- [ ] KMS-backed vault key provider
- [ ] WAF, S3 access logging, explicit KMS key policy, secret rotation (checkov known gaps)
- [ ] External penetration test

## Go-live checklist (per environment)

1. Terraform plan/apply in a staging account; Secrets Manager values; `vp migrate` as owner;
   confirm the runtime role cannot bypass RLS.
2. Put PgBouncer/RDS Proxy in front of Postgres or lower the per-job pool (VALIDATION_REPORT §3.8).
3. LiveKit SIP with a public IP configured (`use_external_ip`/`nat_1_to_1_ip`), carrier trunk
   with REFER enabled, carrier CIDRs as `allowed_addresses`.
4. Run the staging telephony checklist ([docs/telephony-staging.md](docs/telephony-staging.md)).
5. Run both scenario libraries with the production model and a **different** judge model;
   promote only through the gate + human approval.
6. Run a concurrent-call load test against staging (N SIP callers) and set `load_fnc`.
7. Backups: PITR restore drill **including `vp reapply-erasures`**.
8. Dashboard behind SSO/MFA; legal/privacy review (recording consent, cross-border processing
   by providers, CRTC rules for outbound) before calling customers.
