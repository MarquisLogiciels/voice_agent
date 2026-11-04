# Security and privacy

This describes technical controls. **It is not a statement of legal compliance.** Operators
must obtain legal advice for their use (PIPEDA, Québec's Law 25 / Act respecting the protection
of personal information in the private sector, CRTC telemarketing rules, sector rules such as
health privacy acts), publish privacy notices, and run privacy impact assessments where
required (Law 25 requires one before communicating personal information outside Québec).

## Data inventory

| Data | Where | Protection |
|---|---|---|
| Caller phone numbers, names, emails | Postgres (conversations, tool executions) | RLS per tenant, TLS, encrypted storage (RDS + KMS), retention sweep |
| Transcripts, tool events | Postgres `conversation_events` | as above; deleted with the conversation |
| Recordings | S3 `tenants/<tid>/recordings/` | SSE-KMS, TLS-only bucket policy, signed URLs (5 min), audited access, retention |
| Knowledge documents | S3 + chunks in Postgres | tenant prefix + RLS |
| Credentials | Postgres `credentials` | Fernet encryption (key outside DB), write-only API, redaction |
| Audit log | Postgres `audit_logs` | append-only (trigger), tenant-scoped reads |
| Logs / traces | stdout → log platform; OTLP | secrets redacted, phone numbers masked in platform logs, conversation content stripped from traces by default |

Data residency: Terraform defaults to `ca-central-1`. LLM/STT/TTS providers process audio/text
in their own regions — this is a cross-border transfer to disclose and assess (Law 25 PIA).

## Controls mapped to common obligations

* **Accountability / access control**: RBAC (owner/admin/editor/viewer), platform admins,
  API keys can't manage members or keys, audit log of configuration and data access.
* **Consent & transparency**: configurable recording consent announcement before recording
  starts; recording can be disabled per agent; agents identify as AI if instructed (add to the
  agent instructions — recommended).
* **Limiting collection**: prompt rules forbid collecting card numbers, passwords and ID
  numbers; tools only accept the fields they need.
* **Retention & destruction**: tenant `retention_days`, per-agent recording retention, hourly
  sweep; S3 noncurrent versions expire after 30 days.
* **Individual access/erasure**: `GET /data-subjects/export?phone=` returns everything stored
  about a caller; `DELETE /data-subjects?phone=` erases conversations, transcripts, tool payloads
  and recordings (both audited). Records in the tenant's own CRM/calendar must be handled there.
* **Security safeguards**: RLS, encryption, SSRF protection, webhook signatures, rate
  limiting, CSP, least-privilege IAM (task role limited to the `tenants/` prefix).
* **Breach response**: audit log + structured logs support investigation; notification
  procedures are an organizational responsibility.

## Threat model highlights

| Threat | Mitigation |
|---|---|
| Cross-tenant data access via app bug | forced RLS on every tenant table; tests with raw SQL |
| Prompt injection via caller speech, CRM data or documents | platform rules appended after tenant text; tool outputs are data; consequential tools require explicit confirmation + server-side preconditions; tools are scoped to the caller (own appointments, SMS only to caller); no free-form HTTP |
| SSRF through custom integrations or URL ingestion | allowlists, connect-time IP checks (rebinding-safe), no redirects, size limits |
| Duplicate side effects on retries | idempotency ledger in Postgres; vendor idempotency where available |
| Credential leakage | encryption at rest, write-only API, log redaction of decrypted values |
| Toll fraud / unwanted calls | outbound allowlist, DNC, calling window, dry-run, RBAC on outbound endpoint |
| Abuse of the API | JWT/API-key auth, rate limits, login throttling |

## Known gaps

SSO/MFA, KMS-backed vault key provider, automated dependency scanning, external penetration
test.
