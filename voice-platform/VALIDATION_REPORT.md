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
