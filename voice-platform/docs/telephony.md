# Telephony

## Topology

```
Caller ── PSTN ── Twilio Elastic SIP trunk ──► LiveKit SIP ──► dispatch rule (room per call,
                                                               dispatch agent "voice-platform")
                                                              ──► vp-worker job
```

The worker routes by the dialed number (`sip.trunkPhoneNumber` participant attribute) through
`vp_route_inbound_number`, which is the only cross-tenant lookup a call needs.

## Setting up a number

1. Twilio: buy a number, create an Elastic SIP trunk, origination URI = your LiveKit SIP URI
   (`sip:<id>.sip.livekit.cloud`), attach the number. **Enable "PSTN Transfer"** on the trunk if
   you want blind transfers (SIP REFER). `TwilioTrunking.ensure_trunk/attach_number` automates
   this via API.
2. Platform: `POST /v1/tenants/{tid}/phone-numbers` with `provision_livekit: true` creates the
   LiveKit inbound trunk + dispatch rule; optionally pass `allowed_addresses` (the carrier's
   published signaling CIDRs) to reject SIP from elsewhere.
3. Outbound: create a LiveKit outbound trunk (address `<trunk>.pstn.twilio.com`, credentials)
   and store its id on the number (`livekit_outbound_trunk_id`).

## Outbound calls

`POST /v1/tenants/{tid}/calls/outbound {agent_id, to, purpose}`. Guardrails (in order):
do-not-call list (tenant setting), platform allowlist `VP_OUTBOUND_ALLOWED_PREFIXES`, calling
window (default 09:00–21:30 weekdays / 10:00–18:00 weekends, callee time zone; configurable per
tenant), `VP_TELEPHONY_DRY_RUN`. The API creates the conversation and an explicit agent
dispatch; the worker dials with `wait_until_answered` and only then starts the agent. Busy / no
answer / rejected are recorded on the conversation. IVR/voicemail detection is enabled for
outbound calls by default (`conversation.ivr_detection_outbound`).

The calling window and DNC list are technical controls; they are not a substitute for legal
review of telemarketing obligations (CRTC Unsolicited Telecommunications Rules, consent).

## Human transfers

`transfer_to_human(reason, summary, destination?)` → `TransferManager`:

1. Plan: requested or default destination, then its fallback chain (depth-first, cycles
   rejected at config validation), skipping destinations outside their business hours.
2. Announce (`transfer.announce_message`), then attempt each destination:
   * **blind** — `SipService.transfer_sip_participant` (SIP REFER). LiveKit SIP waits for the
     transferee's final NOTIFY: success is a `200` sipfrag (the target answered); `486` and
     timeouts come back as errors (verified in the lab). The tool's time budget is derived
     from the destinations' `ringing_timeout_s`, not the generic `tools.timeout_s`.
   * **warm** — LiveKit `WarmTransferTask`: the caller is put on hold (hold music), a consult
     room dials the human (`wait_until_answered`), the agent briefs them with the handoff
     summary, and only when the human confirms (`connect_to_caller`) are they moved into the
     caller's room. Decline/voicemail/no-answer fail the attempt.
3. SIP outcomes are normalized: 486/600 → busy, 408/480/487 → no_answer, 403/603 → rejected,
   404/5xx → unavailable. The next destination is tried; if all fail the LLM receives
   `ok=false` with `transfer.failure_message` guidance (take a message) — it never claims the
   transfer happened.
4. Every attempt is recorded as a `transfer_attempt` event; the final result is stored on the
   conversation (`transfer`), and Prometheus counts `vp_transfers_total{mode,outcome}`.

Automatic triggers: the caller explicitly asks for a person — with
`transfer.transfer_on_request` (default on) this is detected deterministically (EN/FR,
negation-aware) and the runtime calls `transfer_to_human` itself, because small models picked
the wrong tool in the lab; or N consecutive tool failures
(`transfer.max_failed_tool_calls_before_transfer`) — the executor then tells the agent to offer
a transfer. `transfer_to_human` is available whenever `transfer.enabled` is true.

**Limitations.** Blind transfer requires REFER support on the trunk; some carriers only support
REFER to PSTN numbers, not SIP URIs. Warm transfer requires an outbound trunk
(`transfer.outbound_trunk_id` or `LIVEKIT_SIP_OUTBOUND_TRUNK`). After a successful blind
transfer the call leaves LiveKit; nothing after that (call duration with staff) is observable.
If LiveKit's ringing timeout fires before the carrier stops ringing the target, the caller can
still be connected after the agent has said nobody is available — keep `ringing_timeout_s`
above the carrier's no-answer timeout.

## Recording & consent

`recording.enabled` starts an audio-only room-composite egress to S3
(`tenants/<tid>/recordings/<conversation>.ogg`). With `consent_required`, the consent
announcement is spoken before recording starts. Download links are short-lived signed URLs,
audited, and require the `recordings:read` permission (admin). Retention:
`recording.retention_days` (and the tenant's `retention_days`) enforced by the hourly sweep.

## DTMF

LiveKit delivers DTMF as participant events and offers a `send_dtmf` beta tool for navigating
IVRs on outbound calls; warm transfers support a DTMF string for extensions (`dtmf`).

## Verification status

Lab results (real LiveKit SIP, scripted SIP endpoints, no carrier): `VALIDATION_REPORT.md`
§3.3. Real-carrier verification checklist: [telephony-staging.md](telephony-staging.md) — not
yet performed. Warm transfer, recording and DTMF have not been exercised in either.
