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
