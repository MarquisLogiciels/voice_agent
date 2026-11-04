# Telephony: lab results and staging verification guide

Two environments, deliberately separated:

* **Lab** (reproducible, no carrier, no real numbers): LiveKit server + LiveKit SIP in Docker,
  scripted SIP endpoints standing in for the PSTN caller and the office lines
  (`tests/lab/sipua.py`). Run: `docker compose -f infrastructure/lab/docker-compose.lab.yml up -d`
  then `uv run pytest tests/lab --run-lab`. Results are in `VALIDATION_REPORT.md` §3.3 and
  `docs/evidence/sip_lab.json`.
* **Staging** (real carrier, test numbers you own): the checklist below. Nothing in this
  section has been executed yet — it needs a carrier account and phone numbers.

## What the lab proves and what it cannot

The lab exercises the real LiveKit SIP service, the platform's provisioning code
(`SipProvisioner`), the production worker process (`vp-worker start`), real SIP signalling
(INVITE/REFER/NOTIFY/BYE/CANCEL) and G.711 media. It cannot show: carrier REFER behaviour
(Twilio converts REFER into a new PSTN leg), carrier authentication and IP allowlists, NAT and
public-IP media paths, real phone audio (codecs, jitter, packet loss, echo), DTMF from handsets,
or caller-ID presentation.

## Staging setup (Twilio Elastic SIP Trunking + self-hosted or Cloud LiveKit)

1. **Isolation.** Separate tenant and LiveKit project for staging; set
   `VP_OUTBOUND_ALLOWED_PREFIXES` to the exact test numbers (not a country prefix);
   `VP_TELEPHONY_DRY_RUN=false` only in this environment. Use sandbox integrations or vendor
   sandboxes (HubSpot test portal, Salesforce scratch org/Developer Edition, a dedicated Google
   calendar) — never production CRM credentials.
2. **LiveKit SIP networking (self-hosted).** In the lab, LiveKit SIP advertised `192.0.2.2`
   (a documentation address) in Contact/SDP because no public IP was configured; it worked only
   because everything was on loopback. In staging set `use_external_ip: true` (or
   `nat_1_to_1_ip`), open UDP 5060 (or TLS 5061) and the RTP range to the carrier's signalling
   and media CIDRs only, and pass those CIDRs as `allowed_addresses` when provisioning numbers.
3. **Carrier trunk.** Origination URI → LiveKit SIP. Enable *PSTN Transfer* (REFER) on the
   trunk. Termination: create credentials and an IP ACL; create the LiveKit outbound trunk with
   them (`POST /phone-numbers` → `livekit_outbound_trunk_id`).
4. **Numbers.** Provision through the API (`provision_livekit: true`), which runs the same
   `SipProvisioner` the lab verified. `krisp` noise cancellation is a LiveKit Cloud feature;
   the lab provisioned with `krisp=False` (untested on self-hosted with it enabled).
5. **Transfer timing.** Set each destination's `ringing_timeout_s` *above* the carrier's
   no-answer timeout for REFER legs. In the lab, LiveKit gave up on the REFER at its configured
   10 s while the transferee was still ringing; with a carrier that keeps ringing, the caller
   could be connected to staff *after* the agent said nobody was available. For destinations
   that often don't answer, prefer warm transfer (the platform controls the dial and the
   timeout).

## Staging checklist (expected signals)

| # | Scenario | Do | Expect (platform) | Expect (carrier/LiveKit) |
|---|---|---|---|---|
| S1 | Inbound | Call the number from a test phone; ask "What time do you open on Monday?"; hang up | Conversation `completed`, `direction=inbound`, caller/callee numbers set, transcript has the question, `turn_latency` events | LiveKit room `call-…`, SIP participant with `sip.trunkPhoneNumber` |
| S2 | Blind transfer | Say "Can I speak to a person?" | Announcement played, `transfer_attempt status=completed`, conversation `transferred` | Twilio logs a REFER and a new outbound leg; staff phone rings with the caller's CLI (if carrier preserves it) |
| S3 | Busy → fallback | Make the primary destination busy (decline / DND) | Two `transfer_attempt` events (`busy`, `completed`) | 486 on the first leg |
| S4 | No answer | Let the destination ring out | `no_answer` attempt, agent says `failure_message`, caller stays with the agent | REFER fails with 408/480/487 or times out |
| S5 | Caller hangs up mid-transfer | Hang up while staff phone rings | Conversation finalized (`duration_s` set), no orphaned room | Carrier cancels the REFER leg |
| S6 | Outbound answered | `POST /calls/outbound` to an allowlisted test phone; answer and say "Hello, who is calling?" | Agent introduces itself ("…calling from <business>"), conversation `completed`, `direction=outbound` | INVITE via the outbound trunk |
| S7 | Outbound busy / no answer | Decline / don't answer | `busy` / `no_answer` with `end_reason=dial_*` | 486 / 487 after ringing timeout |
| S8 | Not allowlisted | Outbound to a number outside the allowlist | HTTP 403, no dispatch, audit log entry | no INVITE |
| S9 | Recording | Enable `recording`, call | Consent announcement before egress starts; object in the tenant prefix; signed download URL works | egress completes |
| S10 | DTMF / IVR | Outbound to an IVR test number | IVR detection events | — |

Record for each run: carrier call SIDs, LiveKit room names, platform conversation ids, the
`docs/evidence` JSON produced by the equivalent lab test, and any deviation.
