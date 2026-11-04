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
| Logs / traces | stdout → log platform; OTLP | secrets redacted; phone numbers masked in **every** log line incl. LiveKit's (room names embed the caller number); LiveKit `lk.pii.*` fields (transcripts, tool arguments, chat context, DTMF) dropped unless `VP_LOG_PII` (refused in staging/prod); conversation content stripped from traces by default |

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
  sweep; recordings are purged with **all S3 versions** (a plain delete on the versioned bucket
  only adds a marker).
* **Individual access/erasure**: `GET /data-subjects/export?phone=` returns everything stored
  about a caller; `DELETE /data-subjects?phone=` erases conversations, transcripts (events
  cascade), tool request/response/error text and every recording version; the audit entry keeps
  only a keyed hash of the number. Records in the tenant's own CRM/calendar must be handled
  there.
* **Backups**: RDS automated backups/PITR (7 days, 30 in prod) still contain erased rows until
  they age out. After **any** restore run `vp reapply-erasures`, which re-erases every subject
  recorded in the audit log (matching by keyed hash, across key rotation). Do not restore into
  production without it. Derived data: post-call summaries live on the conversation (erased with
  it); evaluation scenarios are authored or generated from the scenario library, not from
  production transcripts; logs follow the log platform's retention (30 days in Terraform) and
  contain no transcripts or full numbers.
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

## Authentication decisions

* Every request re-reads the user row: disabling a user (or removing a membership) takes effect
  on the next request even though access tokens live up to `VP_JWT_TTL_S` (1 h). There is no
  per-session revocation ("sign out everywhere") yet.
* **SSO/MFA: not implemented in the platform, deliberately.** For a dashboard used by staff of
  small businesses, the robust option is delegating authentication to the customer's identity
  provider (OIDC: Microsoft Entra ID / Google Workspace, with MFA enforced there) or putting the
  dashboard behind an identity-aware proxy (e.g. AWS ALB OIDC authentication, Cloudflare Access).
  Building native TOTP would add a second, weaker credential store to protect. Until one of these
  is in place, treat dashboard access as **not production-ready** for tenants handling sensitive
  data; API keys for server-to-server use are unaffected.

## Known gaps

SSO/MFA (see above), per-session token revocation, KMS-backed vault key provider, external
penetration test, WAF in front of the API (Terraform leaves it as a recommendation). Dependency
and image scanning now run in CI (pip-audit, dependency review, Trivy).
