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
2. **LiveKit server/SIP not runnable here** (no Docker daemon, GitHub releases blocked) — worker
   entrypoint is tested through LiveKit's `fake_job_context` + text sessions, not real rooms.
3. **Evaluation quality depends on real LLMs** — optimization loop logic is tested with
   deterministic LLM doubles; real improvement numbers require API keys.
4. **Blind transfer depends on trunk REFER support** — documented per provider.

## Live checklist
(updated at the end of each session — see bottom of file)
