# Providers and integrations

## Switching AI providers (configuration only)

Each agent version's `pipeline` names providers; the registry turns them into LiveKit objects.

```yaml
pipeline:
  mode: cascaded            # or: realtime (speech-to-speech; see below)
  stt: {provider: deepgram, model: nova-3, language: fr-CA, options: {smart_format: true},
        fallbacks: [{provider: assemblyai}]}
  llm: {provider: openai, model: gpt-4.1, temperature: 0.3,
        connection: {timeout_s: 8, max_retries: 1},
        fallbacks: [{provider: anthropic, model: claude-haiku-4-5}]}
  tts: {provider: elevenlabs, model: eleven_turbo_v2_5, voice: custom_voice_id}
  vad: {provider: silero}
  turn_detection: {mode: model, provider: livekit, model: multilingual}
```

Change `deepgram → assemblyai`, `openai → anthropic`, `elevenlabs → cartesia`, publish a new
version, deploy (or canary it). Running calls keep the version they started with.

Built-in catalogue (`GET /v1/providers` returns capabilities and option schemas):

| Kind | Providers |
|---|---|
| STT | deepgram, assemblyai, openai (batch; wrapped with VAD), google, cartesia, livekit (Inference) |
| LLM | openai, anthropic, google, openai-compatible (`options.base_url`), livekit (Inference) |
| TTS | elevenlabs, cartesia, deepgram, openai, google, livekit (Inference) |
| VAD | silero (local), livekit (Inference) |
| Turn detection | `mode: model` with livekit (local english/multilingual) or livekit-inference; `vad`, `stt`, `manual`, `realtime_llm` |
| Realtime | openai-realtime, google-realtime |

Validation happens before anything is saved: unknown providers, unknown/out-of-range options,
missing voices, non-streaming STT without VAD, `turn_detection: stt` with an STT that doesn't
emit end-of-speech, realtime/cascaded mix-ups.

**Keys.** Platform keys come from environment variables (`OPENAI_API_KEY`, …). A tenant can
bring its own key: create a credential of kind `provider_api_key` and set
`credential: <name>` on the provider entry.

**Fallback & circuit breaking.** Chains become LiveKit `FallbackAdapter`s. A process-wide
circuit breaker records final provider failures across calls and moves a failing provider to
the end of the chain for new calls until its cooldown expires (prevents every new call from
first waiting on a dead provider). Recoverable errors (being retried) don't count.

**Realtime mode** (`mode: realtime`) uses a speech-to-speech model instead of STT+LLM+TTS; it is
validated separately and can't be mixed with cascaded components.

### Adding a provider

1. Write an options model (whitelist of constructor kwargs) and a factory in
   `providers/catalog.py` (or your own module) that imports the LiveKit plugin lazily.
2. `registry.register(ProviderDescriptor(kind=..., name=..., factory=..., capabilities=...,
   secret_env=(...), options_model=..., health_check=...))`.
3. Add a price row to `providers/data/pricing.yaml` (estimates only).
4. Add an instantiation test like `tests/unit/test_providers.py`.

## Business-system integrations

| Kind | Capabilities | Credential kind |
|---|---|---|
| `hubspot` | crm (contacts, deals or leads, notes) | `hubspot` (private-app token) |
| `salesforce` | crm (contacts via SOSL, leads, call tasks) | `salesforce` (client credentials or refresh token) |
| `google_calendar` | calendar (free/busy, events) | `google_service_account` or `google_oauth` |
| `twilio` | messaging (SMS) | `twilio` |
| `custom_rest` | custom actions | `bearer`, `basic`, `api_key_header` |
| `sandbox` | crm + calendar + messaging, in memory | none (dev/staging/evaluations) |

Flow: create credential (`POST /credentials`, write-only) → create integration
(`POST /integrations`) → test (`POST /integrations/{id}/test`, read-only probe) → bind in the
agent config (`integrations: {crm: <name>, calendar: <name>}`) → enable tools.

### Custom REST actions

```yaml
kind: custom_rest
config:
  base_url: https://crm.internal.example.com/api
  allowed_hosts: [crm.internal.example.com]
  auth: {type: bearer}           # token stored in the referenced credential
  actions:
    - name: lookup_customer
      description: Find a customer by phone number
      method: GET
      path: /customers/{phone}
      parameters: [{name: phone, description: "E.164 phone"}]
      response: {fields: {name: data.name, tier: data.tier}}   # only these reach the LLM
    - name: create_ticket
      description: Open a support ticket
      method: POST
      path: /tickets
      parameters: [{name: issue, max_length: 300}]
```

Enable as `tools.enabled: [custom.<integration>.<action>]`. The LLM supplies only parameter
values; writes are always treated as consequential (confirmation required); path values are
URL-encoded (no traversal); hosts are allowlisted; addresses are validated at connect time
(private, loopback, link-local and metadata ranges refused; DNS rebinding blocked); redirects
are not followed; request/response sizes are capped.

### Reliability semantics

* GET/PUT/DELETE and vendor-idempotent writes are retried with full-jitter backoff honoring
  `Retry-After`; 429s are always retried (the request was rejected, not applied).
* A POST that times out is **not** retried; it's reported as *ambiguous* and the agent says a
  team member will confirm. HubSpot/Salesforce adapters reconcile by searching before creating.
* Every consequential tool call is recorded in `tool_executions` with an idempotency key derived
  from (conversation, tool, arguments); repeats replay the stored result.

### Adding an integration

Implement the capability protocol (`integrations/base.py`: `CRM`, `Calendar`, `Messaging`) on
top of `IntegrationHttpClient`, add an `IntegrationKind` entry in `integrations/registry.py`
with a config model and allowed credential kinds, and test it with `httpx.MockTransport`
fixtures shaped like the vendor's documented responses (see `tests/integration/test_integrations.py`).
