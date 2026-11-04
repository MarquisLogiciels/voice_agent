# Operations runbook

## Deploy

1. Build the image (`voice-platform/infrastructure/docker/Dockerfile`, repo root as context,
   LFS files pulled). Model files for the turn detector are downloaded at build time.
2. `vp migrate` with `VP_MIGRATION_DATABASE_URL` (schema owner). Migrations are additive and
   reversible (`alembic downgrade -1`); run before rolling out new code.
3. Roll out api → jobs → worker. Workers drain: LiveKit stops dispatching new jobs to a worker
   receiving SIGTERM and waits for active calls (`drain_timeout=1800s`); ECS `stopTimeout`
   bounds the container — keep scale-in cooldowns long.
4. Smoke: `/readyz`, `/v1/providers/health?probe=true`, one test call.

## Scaling

* **API**: stateless; scale on CPU/latency. Per process ≈160–240 req/s for authenticated reads
  in local measurement (see PRODUCTION_READINESS.md).
* **Worker**: LiveKit runs each call in its own process; capacity per host is bounded by CPU
  (VAD + turn detector run locally) and memory (~200–300 MB per job process observed in text
  mode; measure with audio). LiveKit's `load_threshold` stops accepting jobs on a busy worker;
  autoscale on CPU (Terraform target 55%).
* **Jobs**: scale on queue depth (`select count(*) from jobs where status='queued'`).
* **Database**: pool per process is small (worker job processes cap at 3+2 connections); put
  PgBouncer (transaction mode) in front when worker count × processes grows. RLS context uses
  `set_config(..., true)` (transaction-local), which is compatible with transaction pooling.

## Capacity testing with real audio

Use LiveKit's load tester (`lk load-test`) against a staging project with real providers,
ramping concurrent rooms while watching `vp_response_latency_seconds` p95, worker CPU and
`vp_provider_errors_total`. Record the knee point per worker size before setting autoscaling.

## Backups and restore

* RDS: PITR (prod 30 days), daily snapshots, deletion protection. **Restore drill quarterly**:
  restore to a new instance, run `vp migrate` (no-op), point a staging API at it, verify login
  and a tenant's conversations.
* S3: versioning + lifecycle; recordings/documents restorable within 30 days of deletion
  (noncurrent versions) — note this extends effective retention; adjust for privacy policy.
* Secrets: Secrets Manager versions. Encryption keys: never delete an old key until
  `vp rotate-keys` has re-encrypted all credentials.

## Key rotation

1. `vp gen-key` → prepend to `VP_ENCRYPTION_KEYS` (`new,old`), deploy.
2. `vp rotate-keys` (re-encrypts every credential with the new key).
3. Remove the old key, deploy. JWT secret rotation invalidates sessions (users log in again).

## Incidents

**Investigating a failed call.** Dashboard → Conversations → the call: status, end reason,
transcript, tool events, transfer attempts, latency, cost. Logs: filter by
`conversation_id` (every worker log line carries tenant, agent, version and conversation).
Traces: Jaeger by `conversation_id` attribute on `vp.tool` / `vp.integration` spans plus
LiveKit's own spans (STT/LLM/TTS timing).

**Provider outage** (`ProviderFailing` alert): fallbacks engage automatically and the circuit
breaker moves the dead provider to the end of chains for new calls. If an agent has no
fallback, add one to the config (new version, deploy) or switch providers.

**Transfers failing** (`TransfersFailing`): check trunk REFER support/"PSTN Transfer", the
destination numbers, outbound trunk id for warm transfers, and `transfer_attempt` events
(SIP codes).

**Stuck/dead jobs**: `select kind, status, attempts, last_error from jobs where status in
('running','dead') order by updated_at desc`. Running jobs with expired leases are reclaimed
automatically; dead jobs need a fix then `update jobs set status='queued', attempts=0`.

**Database unavailable**: API `/readyz` returns 503 (load balancer stops routing); live calls
continue (config is held in memory per call) but transcript events buffer in memory and are
retried; tool calls that need the ledger fail safely (no external side effect without a ledger
entry).

**Redis unavailable**: rate limiting falls back to per-process limits; nothing else depends on
Redis.

## Data retention

The job runner enqueues `retention.sweep` hourly: conversations older than the tenant's
`retention_days` are deleted (events cascade), tool payloads scrubbed, and recordings past the
agent's `recording.retention_days` removed from object storage.
