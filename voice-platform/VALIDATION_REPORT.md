# Validation Report

Status labels used throughout:

| Label | Meaning |
|---|---|
| **Implemented** | code exists; no automated test exercises it |
| **Unit tested** | isolated tests with in-process doubles |
| **Integration tested with mocks** | real Postgres/framework, external vendors mocked |
| **Tested against a real sandbox** | real external service or a real self-hosted equivalent (LiveKit server, LiveKit SIP, local models) — *not* the vendor's production cloud |
| **Verified in a real end-to-end environment** | PSTN call through a real carrier and real vendor accounts |
| **Not verified** | not exercised at all |

## 1. Audit baseline (start of mission 2)

Commands run on `claude/epic-sagan-mi2b6m` @ `9764ea4`, clean tree:

| Gate | Result |
|---|---|
| `pytest -q` | 115 passed, 2 skipped (opt-in load tests) |
| `ruff check` / `ruff format --check` | clean |
| `mypy --strict` | clean (102 files) |
| `lint-imports` | 2 contracts kept |

Environment facts (verified, not assumed): no provider, LiveKit Cloud, Twilio, HubSpot,
Salesforce or Google credentials are present; outbound HTTPS to `api.hubapi.com`,
`login.salesforce.com`, `api.twilio.com`, `cloud.livekit.io`, `huggingface.co` and GitHub
release downloads is blocked by the sandbox proxy. Docker Hub and PyPI are reachable and the
Docker daemon can run. Consequently real **self-hosted** equivalents can be exercised
(LiveKit server, LiveKit SIP, open-weights LLM via Ollama, Vosk STT), but no vendor cloud API.

## 2. Findings from source inspection

Defects found by reading the code (not by tests, which passed):

| # | Severity | Finding | Location |
|---|---|---|---|
| F1 | High | Recording-consent announcement is spoken with `session.say()` **before** `session.start()`; on a real call this raises (session not running) and recording would start without the announcement having played | `apps/worker/call.py` |
| F2 | High | Booking checks availability then inserts; two concurrent calls can both pass the check and double-book (no post-insert verification) | `tools/definitions.py::book_appointment` |
| F3 | Medium | Database `IntegrityError` (e.g. racing duplicate names, a phone number owned by a disabled number in another tenant) surfaces as HTTP 500 | API |
| F4 | Medium | Document extraction is synchronous CPU work on the job runner's event loop with no time limit; DOCX (zip) files have no decompression-size guard | `knowledge/extract.py`, `knowledge/service.py` |
| F5 | Medium | Transfer announcement awaits playout from *inside* a tool call; whether LiveKit plays it before the REFER (vs. deadlocking behind the current speech) was never exercised | `apps/worker/call.py::announce` |
| F6 | Medium | Model-based turn detection (default in `sample_config`) needs model files from Hugging Face; workers without them fail at session build. No graceful fallback | provider catalog |
| F7 | Low | `OpenAICompatibleOptions.base_url` requires `https://`, which blocks self-hosted models on a private network | provider catalog |

Architectural weaknesses:

* All "live" behaviour (rooms, audio, SIP, transfers, egress) was untested; the worker's
  `CallHandler` had only a stand-in `JobContext`.
* Evaluation and optimization were demonstrated only with a scripted model.
* Vendor adapters were tested against fixtures I wrote from memory of the vendor APIs.
* The 100-conversation degradation was measured inside **one** event loop, which is not how
  LiveKit executes jobs; no conclusion about per-host capacity can be drawn from it.

Security/reliability risks noted for the hardening pass: unbounded extraction (F4), booking
race (F2), JWTs not revocable before expiry, no dependency vulnerability scanning, no MFA.

## 3. Work log and evidence (this mission)

Sections below are appended as each validation is performed, with the exact commands and
results. Anything not listed here keeps the label it had in PRODUCTION_READINESS.md.

### 3.1 Self-hosted lab stack

`infrastructure/lab/docker-compose.lab.yml`: LiveKit server 1.13.11 (`--dev`), LiveKit SIP
v1.23.0, Redis, Ollama serving **Qwen 2.5 3B Instruct Q4_K_M** (CPU, 4 vCPU), Vosk servers
(kaldi English and French models). Speech for callers is synthesized with SVOX Pico.
New platform providers (Implemented + Unit tested + Tested against a real sandbox):
`stt/vosk` (streaming, manual flush), `tts/pico`, `tts/espeak`.

Security fix made while wiring this up (Unit tested): the `openai-compatible` LLM sent the
**platform** API key to whatever `base_url` the tenant configured (key exfiltration + SSRF).
Platform keys now go only to `VP_TRUSTED_PROVIDER_ENDPOINTS`; tenant endpoints require a
tenant (BYO) credential and must be public HTTPS addresses (resolved-IP check). Vosk URLs
must be trusted endpoints.

F1 fixed: the consent announcement is now spoken by the agent's `on_enter` (session
running) and egress starts only after it has played (bounded wait).
F7 fixed: `base_url` accepts `http://` for trusted platform endpoints only.

### 3.2 Live voice conversation through LiveKit — **Tested against a real sandbox**

`uv run pytest tests/lab/test_live_call.py --run-lab` (services from the lab compose file).

Nothing in the call path is mocked: a `livekit.rtc` participant publishes synthesized
speech as a WebRTC/Opus track and records the agent's track; the agent is the real
`vp-worker start` process (production process model: one forkserver process per job)
dispatched with explicit agent dispatch; config, deployment, conversation and events live
in a real Postgres with RLS. The test asserts what a caller would experience: the greeting
is intelligible (agent audio re-transcribed by Vosk), the factual answer is correct, the
availability question produces a `check_availability` call against the sandbox calendar
and the returned times are **spoken back**, talking over the agent stops it, and hanging
up finalizes the conversation (`completed`, `participant_disconnected`, `channel=web`).

Repeated runs after the fixes below (same code, 2026-11-04):

| Run | Greeting WER | Turn 1 response (s) | Turn 2 response (s) | Barge-in → agent silent (s) | Tool call | Needed follow-through |
|---|---|---|---|---|---|---|
| 1 | 0.08 | 3.75 | 2.69 | 0.65 | `check_availability(date="next monday")` ok | yes |
| 2 | 0.08 | 3.40 | 2.63 | 0.68 | same | yes |
| 3 | 0.08 | 3.55 | 2.25 | 0.69 | same | yes |
| 4 | 0.00 | 4.54 | 2.82 | 0.69 | same | yes |

"Response" is measured at the caller: end of the caller's audio → first agent audio.
Per-turn breakdown persisted by the platform (run 1, p50): end-of-utterance 1.04 s,
LLM TTFT 2.44 s (CPU), TTS TTFB 0.03 s. Evidence file: `docs/evidence/live_call.json`.
The greeting WER of 0.08 is Vosk hearing "alright" for "Hi," in robotic Pico audio.

**Defects found only by running the real call** (all fixed, each with a regression test):

| # | Severity | Defect | Fix |
|---|---|---|---|
| L1 | Critical | Worker entrypoint was a closure inside `build_server()`; LiveKit's process executor pickles it by name, so in production **no job process could start** (every call would fail). Never caught because no test ran `vp-worker start`. | Module-level entrypoint; `test_worker_process_model.py` |
| L2 | High | LiveKit `AgentServer` routes its server connection through `HTTPS_PROXY` and ignores `NO_PROXY`; with an egress proxy a self-hosted LiveKit was unreachable (HTTP 405 from the proxy). | Worker resolves the proxy honouring `NO_PROXY` and passes it explicitly |
| L3 | High | Vosk transcribes near-silence and post-silence onsets as "the" (verified: any noise ≥ ±1 LSB, and ≥ 4 s of digital silence before speech). Each ghost word became a user turn and cancelled the LLM generation (first response 27 s). | Noise gate + silence dropping with timestamp re-mapping in `VoskStream` |
| L4 | Medium | Vosk finalizes only on its own endpointer, adding ~1.9 s per turn. | `manual_flush` capability: LiveKit flushes at end of turn → `eof` + reconnect (EOU p50 1.9 s → 1.0 s) |
| L5 | Medium | Opening hours reached the prompt as `fri: 09:00-17:00; mon: …` (JSONB alphabetic key order); the model read it verbatim and TTS said "nine minus seventeen". | Hours rendered as speech, weekday order, grouped |
| L6 | Medium | With Qwen 2.5 3B the agent says "Let me check, hold please" and **never calls the tool** (0/5 single-turn probes with the default platform prompt); the caller waits in silence. | Follow-through guard (one follow-up generation per caller turn; recovered 4/4 in probes and in every live run) + tenant instructions written as a procedure |
| L7 | Medium | The model's date arithmetic is wrong (asked on a Wednesday: "next Monday" → Sunday 11-08, Tuesday 11-10, Wednesday 11-11, Saturday 11-14 in different runs). | Tool schema asks for the caller's words; platform resolves dates deterministically (EN/FR); unambiguous weekday contradictions are corrected and the result states the resolved day |
| L8 | Medium | First turn took 12–27 s: the ~900-token prompt is cold in the LLM's cache (it embeds the current minute, so it differs per call). | Optional `conversation.prewarm_llm_prompt` prefills during the greeting (first response ~3.8 s) |
| L9 | Low | Prewarm request used LiveKit's default 10 s timeout and retried, logging an LLM error into the conversation. | Uses the agent's LLM timeout, no retry |

Observed but not changed (documented): LiveKit's default load function is whole-host CPU;
with the LLM on the same host the worker marked itself "at full capacity" six times per
call. Production workers must not share hosts with inference servers, or must set
`load_fnc`. The lab test waits up to 8 s of silence after "one moment" because CPU decoding
of the tool call and the answer takes ~5 s each; on GPU/cloud models this is sub-second.

What this does **not** show: PSTN/SIP ingress (see 3.3), commercial STT/TTS/LLM quality or
latency, behaviour under concurrent calls, or robustness to real caller audio (accents,
line noise) — synthetic speech is far cleaner than a phone line.

### 3.3 Telephony through LiveKit SIP — **Tested against a real sandbox** (no carrier)

`uv run pytest tests/lab/test_sip_lab.py --run-lab` — real `livekit/sip` v1.23.0 and LiveKit
server; numbers provisioned with the platform's `SipProvisioner` (inbound trunk + dispatch
rule per test, removed afterwards); outbound trunk to the scripted "office" endpoint; the
production worker process; outbound calls placed through `POST /calls/outbound` with the
real guardrails (allowlist `+1514555`, calling window, not dry-run). The PSTN caller and the
office lines are `tests/lab/sipua.py`: a scripted SIP/RTP user agent (UDP, G.711 µ-law, REFER
transferee) written for this lab because baresip cannot initialise without IPv6 sockets in
this sandbox. Media is real RTP; what each side *hears* is re-transcribed with Vosk.

Result of the full run (7/7 passed, 8 min 14 s; `docs/evidence/sip_lab.json`):

| Scenario | Signalling observed | Platform outcome |
|---|---|---|
| Inbound call, question, hang-up | INVITE → 100/180 → 200 (0.97 s) → ACK … BYE/200 | `completed`, `inbound`, `phone`, caller/dialed numbers stored; greeting heard with WER 0.00 over G.711; answer "we open at nine am on monday"; caller-perceived response 4.5 s |
| Blind transfer | REFER `Refer-To: tel:+15145550177` → 202; NOTIFY 100, NOTIFY 200; LiveKit BYEs its leg | `transferred`, attempt `front_desk: completed`; the caller hears the staff member ("hi this is [Sam] at the front desk") on the new leg |
| Busy → fallback | REFER #1 → NOTIFY 486; REFER #2 (backup) → NOTIFY 200 | attempts `front_desk: busy`, `backup: completed` |
| No answer | REFER → LiveKit gives up after the configured 10 s ("request timed out"); transferee's NOTIFY 487 arrives later | attempt `no_answer`; the caller hears the announcement, then "sorry, nobody is available … take a message"; call continues |
| Caller hangs up during transfer | REFER, caller CANCELs the new leg and BYEs | conversation finalized (`completed`, duration set), no room left with participants |
| Outbound, answered | INVITE from LiveKit via the outbound trunk → 180 → 200 | callee says "hello who is calling please" over the greeting; agent: "This is Alex from Maple Dental. How can I assist you today?"; `completed`, `outbound` |
| Outbound, busy | INVITE → 486 | `busy`, `end_reason=dial_busy` |

**Defects found only by these runs** (fixed, each with a regression test):

| # | Severity | Defect | Fix |
|---|---|---|---|
| T1 | High | `transfer.transfer_on_request` (default true) was **read nowhere**; "Can I speak to a person?" depended on the model, and Qwen 2.5 3B called `check_availability` instead | Deterministic escape hatch (`runtime/escalation.py`, EN/FR, negation-aware): runs `transfer_to_human` through the executor, skips the LLM turn, speaks `failure_message` if all attempts fail |
| T2 | High | `transfer_to_human` existed only if listed in `tools.enabled`, while `transfer.enabled` made the prompt tell the model to use it | Tool availability follows `transfer.enabled` |
| T3 | High | Every tool, including transfers, ran under `tools.timeout_s` (8 s); the tool reported failure (agent: "nobody is available") while the SIP transfer was still ringing and could still connect | Per-tool time budget; transfers get 30 s + Σ(ringing_timeout + 5 s) |
| T4 | Medium | Outbound callees heard the inbound greeting ("thanks for calling …"); asked who was calling, the agent said "it's a lab call" | Direction-aware greeting (`greeting.outbound_text`, default "Hi, this is <persona> calling from <business>") and an outbound prompt rule |
| T5 | Low | Follow-through did not cover narrated transfers ("let me transfer you") | Patterns added |

Still open / documented: LiveKit's REFER ringing timeout can expire before the carrier stops
ringing the target (the lab transferee rang 12 s vs LiveKit's 10 s), so a caller might be
connected after hearing the failure message — keep `ringing_timeout_s` above the carrier's
no-answer time or use warm transfer (`docs/telephony-staging.md`). LiveKit SIP advertised
`192.0.2.2` in Contact/SDP without a configured external IP; harmless on loopback, fatal
behind NAT. **Not exercised:** warm transfer (`WarmTransferTask`), recording/egress, DTMF,
SIP auth/IP allowlists, any real carrier. An LLM-initiated transfer (announcement played from
inside a tool call, audit finding F5) was not exercised because the escape hatch now handles
explicit requests; reading LiveKit's scheduler shows a `say()` created inside a tool can play
while the tool runs (the scheduler waits only for the current speech's generation), but this
is **Not verified**.

### 3.4 Audit fixes F2–F4 — **Unit / Integration tested**

* F2 booking race: after inserting, the tool lists overlapping live appointments; if any
  exist, the smallest id survives and the others cancel their own event and tell the caller
  the slot was just taken. Regression test with a Google-like calendar that accepts
  overlapping inserts and a slow insert (fails without the fix, passes with it). Residual
  risk: a calendar whose list is not read-after-write consistent.
* F3: `IntegrityError` anywhere (handler or commit in a dependency teardown) → HTTP 409
  `conflict`, no SQL or values echoed. Test races two identical phone-number creates
  (`[201, 409]`, previously 500 for the loser).
* F4: extraction runs in a spawned process killed at 60 s; DOCX zip entries/uncompressed
  size/ratio bounded before parsing; extracted text capped. Tests: a 120 MB zip bomb that is
  ~120 KB on disk is refused; timeout kills the child.

### 3.5 Vendor contracts — Google Calendar & HubSpot **machine-checked against vendor specs**; Salesforce **reviewed against documentation**

`tests/integration/test_vendor_contracts.py` drives the real adapter methods through an HTTP
recorder and validates every request against the vendor's own artefacts: the Calendar v3
discovery document bundled in `google-api-python-client` (method, verb, path template, query
parameters and enums, body schema incl. nested types) and the request models of HubSpot's
OpenAPI-generated `hubspot-api-client` (wire names, types, enums with client-side validation).
The validators were checked to reject wrong field names, wrong types and invalid enums
(`operator: "EQUALS"`). Salesforce has no bundled machine-readable spec; the adapter's
external-ID upsert, 201/200/204 handling and SOSL usage were compared with Salesforce's
documentation (upsert matching rules; External ID/Unique requirement noted in the setup docs).
None of this replaces a run against a vendor sandbox account, which remains **Not verified**.

### 3.6 Repository hygiene finding

The repository-root `.gitignore` (upstream LiveKit) contains `docs/`, so **none of
`voice-platform/docs/` had ever been committed** although the README and readiness documents
link to it. Fixed with a scoped `!docs/` in `voice-platform/.gitignore`; the docs and the lab
evidence files are now in the repository.

### 3.7 Dependency and image scanning

CI now runs `pip-audit` on the locked third-party set (`uv export --no-emit-local`; run
locally on 2026-11-04: 157 packages, **no known vulnerabilities**), GitHub dependency review on
pull requests (fails on high severity), and a Trivy scan of the built image (critical/high,
fixed-only). The Trivy step has not been run here (no image build yet at time of writing).

### 3.8 The "100 conversations" degradation and per-host capacity — **analysis + partial measurement**

The mission-1 load test ran 10/50/100 *text* conversations inside **one** event loop and saw
platform overhead p95 rise to 0.58 s at 100. That set-up does not match production, and the
lab now proves how production runs:

* **Process model (verified).** `vp-worker start` runs each job in its own process (LiveKit
  `JobExecutorType.PROCESS`, forkserver). Defect L1 (unpicklable entrypoint) could only exist
  because of this, and every lab call ran in a separate job pid. One call never shares an event
  loop with another, so the single-loop curve says nothing about a worker host.
* **Idle processes.** In production mode LiveKit keeps `num_idle_processes = cpu_count`
  warm processes (4 on a 4-vCPU host) so calls start without import/VAD-load delay (lab:
  dispatch → greeting audio 1.2–1.6 s).
* **Memory per job process (measured).** Importing LiveKit + platform + loading Silero +
  provider plugins: **≈ 220 MB RSS** before a call (RSS includes shared pages; PSS is lower).
  `job_memory_warn_mb=700` is set.
* **CPU / admission (verified behaviour).** LiveKit's default `load_fnc` is whole-host CPU
  averaged over ~2.5 s with `load_threshold=0.7`; in the lab, CPU inference on the same host
  made the worker flap to "full capacity" six times per call. LiveKit's published guidance:
  4 cores / 8 GB per agent server handles **10–25 concurrent jobs**; their reference test ran 30
  VAD-only sessions at 3.8 cores / 2.8 GB. With ~220 MB per process the platform's memory
  budget supports ~25 jobs per 8 GB host, CPU being the first limit with Silero VAD per call
  (and more if the LiveKit turn-detector model or noise cancellation runs on the host).
* **New capacity risk found: database connections.** Each job process gets its own pool
  (`db_pool_size ≤ 3`, `db_max_overflow ≤ 2`, `apps/worker/main.py`) → up to **5 connections
  per call**, i.e. ~125 per fully loaded host plus API/jobs. Three hosts at 25 calls already
  exceed a `db.t4g.medium` RDS `max_connections` (~400 incl. reserved). Recommendation: put
  PgBouncer (transaction pooling) or RDS Proxy in front of Postgres and/or lower the per-job
  pool to 1 + 1. Tenant scoping uses transaction-local `set_config(..., true)`, so transaction
  pooling is compatible; with asyncpg add `?prepared_statement_cache_size=0` to the URL.

**Recommendation (not a measurement):** size worker hosts at 4 vCPU / 8 GB, start with
`load_fnc` = active jobs / 15 and `load_threshold` 0.9 (predictable admission instead of
CPU spikes), keep inference servers off worker hosts, and run a real concurrent-call load test
(N simulated SIP callers against a staging LiveKit + cloud providers, watching
`vp_response_latency_seconds` p95, job RSS and DB connections) before choosing the number.
**Not measured here:** concurrent calls on one worker (this 4-vCPU box is saturated by the
local LLM alone, so any number would describe Ollama, not the platform).

### 3.10 Audio-level simulation — **Tested against a real sandbox** (synthetic callers)

`uv run pytest tests/lab/test_audio_harness.py --run-lab` drives the platform's own Vosk
provider (`VoskSTT.stream()`: noise gate, silence dropping, manual flush, ghost-word filter)
and Silero VAD through LiveKit's stream interfaces. Callers are synthesized (SVOX Pico,
eSpeak NG); conditions are applied with deterministic seeds. Evidence:
`docs/evidence/audio_harness.json` (every reference/hypothesis pair).

| Condition | WER | Utterances |
|---|---|---|
| en/pico | 0.9 % | 10 |
| en/pico x0.8 (slower) | 3.1 % | 10 |
| en/pico x1.3 (faster) | 6.1 % | 10 |
| en/pico SNR 20 dB | 3.2 % | 10 |
| en/pico SNR 10 dB | 16.8 % | 10 |
| en/pico SNR 5 dB | 80.0 % | 10 |
| en/pico telephone (8 kHz G.711) | 18.1 % | 10 |
| en/espeak en-us | 24.1 % | 10 |
| en/espeak Scottish | 43.9 % | 10 |
| en/espeak Caribbean | 39.6 % | 10 |
| fr/pico | 0.0 % | 8 |
| fr/pico SNR 10 dB | 9.4 % | 8 |
| fr/pico telephone | 4.2 % | 8 |
| fr/espeak fr-FR | 26.5 % | 8 |
| fr/espeak fr-BE | 31.2 % | 8 |

* Turn boundaries (Silero, LiveKit defaults, clean + 10 dB): speech start detected p50 0.09 s
  after true onset; end of speech p50 0.69 s / max 0.78 s after true end (includes the
  min-silence window); no utterance missed.
* Flush → final transcript in real time: p50 0.49 s, max 1.37 s.
* False triggers on 10 s of non-speech: without gate/filter Vosk returns "the" for silence,
  −50 dBFS and −35 dBFS noise; with the platform defaults it returns nothing in all three
  (VAD reported no speech in any of them). The gate alone fixes silence but not steady noise —
  hence the lone-word filter.

Findings: (1) the wideband English Vosk model loses ~17 points of WER on 8 kHz phone audio
(band-limited resampling did not change this) while the French model barely degrades — for
phone lines use a telephony/narrowband model or a commercial STT; (2) synthetic voices are an
easy case: eSpeak's robotic voices already cost 24–44 %, real accents and phone lines will be
worse; (3) **no Québec French voice was available offline** — French was tested with Québec
vocabulary spoken by European voices, so Québec accents are **Not verified**.

### 3.11 Privacy, logging and erasure — fixes found by inspecting real artefacts

* Lab worker logs contained the caller's phone number (`lk.pii.participant_identity`, and in
  every line via LiveKit's room name `call-_+15145550199_…`). LiveKit tags transcripts, tool
  arguments, chat context and DTMF as `lk.pii.*`. Fix: redaction filter attached to *all*
  root handlers, including LiveKit's in the worker main process (which re-emits job-process
  records with `callHandlers`, bypassing logger filters); `lk.pii.*` dropped unless
  `VP_LOG_PII` (refused in staging/prod); E.164 numbers masked in every string. **Unit tested.**
* Erasure on the versioned S3 bucket only added a delete marker (recordings recoverable for
  30 days). Now every version and marker is deleted; IAM grants `DeleteObjectVersion` /
  `ListBucketVersions` on the tenant prefix. **Integration tested with mocks** (moto, versioned
  bucket: two versions survive `delete`, none survive `purge`).
* S3 uploads forced `ServerSideEncryption=AES256`, overriding the bucket's KMS CMK default.
  Removed.
* Backups: erased subjects are recorded as keyed hashes; `vp reapply-erasures` re-erases them
  after a restore. **Integration tested** (erase → simulated restore → reapply → gone); a real
  RDS restore drill is **Not verified**.

### 3.12 Infrastructure validation

* Terraform 1.17.1 + AWS provider 5.100.0 (from releases.hashicorp.com, SHA256-verified, via a
  filesystem mirror because the registry is blocked): `fmt -check` and `validate` **pass**.
  Checkov 3.4.3: 120 passed / 27 failed initially → fixes (SG descriptions, ALB egress limited
  to the API tasks, RDS log export + minor upgrades + PI CMK, Redis CMK, abort incomplete
  uploads, prod deletion protection) and justified inline skips → **132 passed, 4 known gaps**
  (S3 access logging, explicit KMS key policy, secret rotation, AZ pinning). CI enforces this.
  `plan`/`apply`: **Not verified** (no AWS account).
* Compose: `docker compose config` valid for the app and lab stacks; the lab stack ran for the
  whole session. Worker/jobs containers no longer inherit the API health check; the worker's
  check uses LiveKit's `:8081` health server (503 until registered).
* Graceful shutdown: the worker drains calls for up to 30 min on SIGTERM, but Fargate kills a
  container 120 s after SIGTERM, so every deployment would have cut calls longer than 2 min.
  The worker now enables ECS task scale-in protection while it has active jobs
  (`apps/worker/ecs_protection.py`, unit tested against a fake ECS agent endpoint; **Not
  verified** on ECS).
* Docker image build: **not performed** — the Docker daemon in this sandbox stopped at its
  2-hour limit and could not be restarted; the lab containers kept running without it (port
  forwarding and log draining done manually). CI builds and Trivy-scans the image.
* F6 (turn detector): LiveKit registers the turn-detector inference runner only when the plugin
  is imported in the worker's main process; the platform imported it lazily in job processes,
  so model turn detection (the sample default) could not work and `download-files` fetched no
  model. Fixed (`register_main_process_plugins`), with VAD fallback when disabled.
  **Unit tested**; with the real model files **Not verified** (Hugging Face blocked).

### 3.13 Upstream LiveKit issues observed (for `lk docs submit-feedback`; CLI not installable here)

1. `AgentServer` uses `HTTPS_PROXY`/`HTTP_PROXY` for its server connection and ignores
   `NO_PROXY`; a self-hosted LiveKit on an internal address becomes unreachable behind an egress
   proxy (HTTP 405 from the proxy). Workaround in `apps/worker/main.py::worker_http_proxy`.
2. The turn-detector plugin must be imported in the worker's main process for its inference
   runner (and `download-files`) — easy to miss when providers are constructed lazily; the
   failure only appears at runtime.
3. The SIP individual dispatch rule names rooms `<prefix>_<caller number>_<random>`, so the
   caller's phone number is in every job log line (`room`) and in room listings; an option to
   omit the number would help data minimisation.
4. LiveKit's CPU-based default `load_fnc` measures the whole host; co-located inference makes
   the worker flap between available/full (six times per call in the lab).
5. `TransferSIPParticipant` returns "request timed out" at its ringing timeout while the
   transferee may still be ringing the target; documenting this race would help.

## 4. Acceptance criteria for production (proposed)

Each criterion names how it is measured; "Status" is where the platform stands **today**.

| Area | Criterion | How measured | Status |
|---|---|---|---|
| Call handling | 99.5 % of inbound calls answered by an agent within 3 s of INVITE | staging SIP load test + `vp_call_setup_seconds` | lab: 0.97 s, single call |
| Responsiveness | p95 caller-perceived response ≤ 1.5 s, p50 ≤ 1.0 s (cloud STT/LLM/TTS) | `vp_response_latency_seconds` in staging | lab (CPU LLM): 2.3–4.5 s — **not met / not representative** |
| Barge-in | agent silent ≤ 0.8 s after the caller starts talking | lab live-call test | **met** in lab (0.65–0.69 s) |
| Recognition | WER ≤ 12 % on recorded real phone calls (EN and Québec FR) | labelled call sample | **not measured**; synthetic 8 kHz EN with Vosk 18 % |
| Business correctness | critical scenarios ≥ 95 % pass over ≥ 3 repetitions, Wilson lower bound ≥ 0.85; zero false success claims | `bench_real_model` with the production model, independent judge | §3.9 (3B model is far below) |
| Safety | zero bookings/leads without explicit confirmation; zero prompt-injection compliance in the robustness suite | deterministic checks | §3.9 |
| Transfers | ≥ 99 % of transfer requests reach a human or a fallback; failure message always spoken | staging telephony checklist S2–S5 | lab: 4/4 scenarios correct |
| Duplicates | zero duplicate vendor records under retry/timeout/restart tests | workflow test + staging fault injection | **met** in tests (HubSpot requires `idempotency_property`) |
| Isolation | zero cross-tenant reads in RLS tests and pen test | test suite + external pen test | tests pass; pen test **not done** |
| Privacy | no transcripts or full phone numbers in logs; erasure removes every artefact incl. after restore | log scan in staging; restore drill | tests pass; drill **not done** |
| Deploys | no call dropped by a deployment | rolling deploy during a live test call | ECS protection implemented, **not verified** |
| Capacity | documented calls/host at p95 targets; DB connections < 70 % of max at peak | concurrent-call load test | **not measured** |
