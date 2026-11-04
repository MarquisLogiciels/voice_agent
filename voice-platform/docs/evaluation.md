# Evaluation and optimization

## Scenarios

YAML (`scenarios/<suite>/*.yaml`) or stored per tenant (`POST /scenarios`):

```yaml
- id: book-simple
  name: Caller books a cleaning
  critical: true                 # must never regress
  now: "2030-01-07T13:00:00+00:00"   # pinned clock → reproducible availability
  persona: {name: Ana Roy, mood: neutral, language: en}
  script:                        # deterministic caller (or `simulated:` for an LLM caller)
    - say: "I'd like to book a cleaning on 2030-01-08 in the morning."
      expect_agent: ["available|opening"]
    - say: "The first one works. My name is Ana Roy."
    - say: "Yes, that's correct."
  sandbox: {contacts: [], busy: [], faults: {create_appointment: {error: unavailable, times: 1}}}
  transfer_sim: {outcomes: {front: busy}}
  knowledge_documents: {Price list: "A cleaning costs 120 dollars."}
  expectations:
    tools: [{name: book_appointment, max_calls: 1}]
    forbidden_tools: []
    transfer: false
    state: {appointments: 1, appointment_attendee_contains: Ana}
    agent_must_mention: []
    agent_must_not_mention: ["you're booked"]
  judge_rubric: ["empathy: acknowledges the caller's concern"]
```

Simulations run the **real** `PlatformAgent`, tool executor and transfer manager in LiveKit's
`AgentSession` (text mode). Integrations are bound to an in-memory sandbox seeded from the
scenario (with fault injection), telephony to a simulated transport, persistence to memory —
simulations cannot reach production systems or dial anyone.

The bundled `scenarios/receptionist` suite covers booking, missing information, change of mind,
taken slots, human requests, busy transfer fallback, CRM outage, calendar timeout, a French
(Québec) booking and a knowledge question.

## Scoring

Deterministic checks first (critical unless noted): expected tool calls and arguments, forbidden
tools, sandbox side effects (appointments/leads/contacts/SMS), transfer outcome and
destination, **false success claims** (the agent says "booked/sent/cancelled" without a
successful tool result), internal errors; non-critical: mentions, per-turn reply patterns,
repetition, turn budget, latency, repeated (deduplicated) actions.

The optional LLM judge scores a rubric 1–5. It must use a model different from the agent's
(the job runner skips the judge if they are the same). A scenario passes when all critical
checks pass and the judge (if used) scores ≥ 3. Summaries report pass rate with a 95% Wilson
interval, category scores, failure categories, tool success and latency percentiles.

## Running

* Dashboard → Evaluations, or `POST /evaluations {agent_id, version_id, scenario_slugs,
  repetitions, seed}` (job runner executes it with the version's real providers, the
  simulator LLM `VP_SIMULATOR_LLM` and judge `VP_JUDGE_LLM`).
* Locally: `vp eval --config examples/front-desk.fr-ca.yaml --judge anthropic:claude-haiku-4-5`.
* Compare two runs: `GET /evaluations/{baseline}/compare/{candidate}`.

## Optimization loop

`POST /optimizations {agent_id, baseline_version_id, scenario_slugs, max_iterations,
min_pass_rate, allowed_models}`:

1. Baseline benchmark (seeded) → failure report (categories + examples).
2. Proposers: the heuristic proposer maps failure signatures to documented remedies; if
   `VP_OPTIMIZER_LLM` is set, an LLM proposer suggests patches. **All patches are re-validated**
   against an allowlist: instructions, persona style, greeting, tool description overrides,
   LLM temperature, endpointing/interruption parameters within bounds, retrieval settings, tool
   step limit, and LLM model only from `allowed_models`. Integrations, credentials, providers,
   transfer numbers, recording and code are never touched.
3. Candidate re-run on the identical seeded suite. A candidate is kept if it has no scenario
   regressions, no critical regressions, no p95 latency regression beyond 0.5 s, and either a
   higher pass rate or (for complementary partial fixes) a higher mean score.
4. Final gate vs. the original baseline: pass rate ≥ `min_pass_rate` and strictly better.
5. Result: a `candidate` version (parent = baseline, source = optimizer) and a markdown report.
   **Nothing is deployed** until an admin approves (`POST /optimizations/{id}/approve`), which
   publishes the candidate and rolls it out as a canary (default 10%) or full deploy; rollback
   is one call. Rejecting marks the candidate rejected.

Statistical caution: with few scenarios/repetitions the Wilson interval is wide; use
`repetitions ≥ 3` with real LLMs (non-deterministic) before trusting small deltas.
