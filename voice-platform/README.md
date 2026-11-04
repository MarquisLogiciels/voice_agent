# Voice Platform

Multi-tenant platform for building, running, evaluating and improving AI voice agents
(receptionists and other "virtual employees") on top of **LiveKit Agents**.

* Swap STT / LLM / TTS / VAD / turn detection per agent through configuration, with fallback
  chains and a cross-call circuit breaker — no application code changes.
* Real-time business tools (CRM, calendar, SMS, custom REST) behind a guarded executor:
  validation, confirmation, idempotency, timeouts, audit.
* Telephony via LiveKit SIP: inbound routing by number, outbound dialing with guardrails,
  blind (SIP REFER) and warm transfers with fallback destinations, egress recording.
* Knowledge bases with hybrid (pgvector + stemmed full-text) retrieval.
* Versioned agent configs, deployments per environment, canaries, rollback; config pinned per call.
* Evaluation (scenario suites, simulated callers, deterministic checks + independent LLM judge)
  and an optimizer that proposes allowlisted config changes, rejects regressions and requires
  human approval to promote.
* Tenant isolation enforced by PostgreSQL row-level security; RBAC; encrypted credential vault.

Read [ARCHITECTURE.md](ARCHITECTURE.md), [DECISIONS.md](DECISIONS.md),
[IMPLEMENTATION_PLAN.md](IMPLEMENTATION_PLAN.md) and
[PRODUCTION_READINESS.md](PRODUCTION_READINESS.md). Guides live in [docs/](docs/).

## Quick start (local)

Requirements: Python 3.11+, [uv](https://docs.astral.sh/uv/), PostgreSQL 16 with pgvector
(or Docker), Node (only for the browser smoke test). The repository must be cloned with Git LFS
(`git lfs pull`) so the Silero VAD model is present.

```bash
cd voice-platform
uv sync
uv run pytest -q                 # starts a throwaway Postgres automatically if binaries exist
```

Full stack with Docker (Postgres, Redis, MinIO, LiveKit dev server, API, worker, jobs,
Prometheus, Grafana, Jaeger):

```bash
cp voice-platform/.env.example voice-platform/.env   # add provider API keys
docker compose -f voice-platform/infrastructure/docker-compose.yml up --build
docker compose -f voice-platform/infrastructure/docker-compose.yml exec api \
  vp bootstrap --email admin@example.com --password 'a-long-password-here'
```

Then open <http://localhost:8000/dashboard/> (API docs at `/docs`, Grafana at :3000,
Jaeger at :16686).

## Processes

| Command | Purpose |
|---|---|
| `vp-api` | management API + dashboard + webhooks |
| `vp-worker start` | LiveKit AgentServer executing calls (`dev` for hot reload) |
| `vp jobs` | background jobs: ingestion, evaluations, optimizations, retention |
| `vp migrate` | apply database migrations (schema owner role) |
| `vp bootstrap` | create/promote a platform administrator |
| `vp eval --config agent.yaml` | run the scenario suite locally against real providers |
| `vp rotate-keys` / `vp gen-key` | credential key rotation |

## Quality gates

```bash
uv run ruff check src tests && uv run ruff format --check src tests
uv run mypy                       # strict
PYTHONPATH=src uv run lint-imports  # architecture contracts
uv run pytest -q                  # unit + integration + e2e + evaluations
uv run pytest tests/load --run-load -s   # load measurements (writes docs/load-test-results.json)
```
