# Optimization report

- Decision: **no_improvement** (production promotion still requires human approval)
- Baseline pass rate: 57% (95% CI 42%–71%) over 42 runs

## Baseline failure categories

- tool: 19
- state: 16
- mentions: 3
- simulation_completed: 2
- transfer: 2
- no_false_success_claims: 1

## Iterations

### Iteration 1 (heuristic) — rejected
- `append_text` `instructions`: 'Once the caller agrees to a specific slot and you have their name and the reason for the visit, read the details back, get a clear yes, then call book_appointme' — bookings were not completed
- pass rate Δ -0.05; regressions: ['faq-price', 'slot-taken']; improvements: none
- gate: no improvement over baseline (0.57 → 0.52); scenario regressions: ['faq-price', 'slot-taken']; p95 turn latency regressed 21.10s → 22.09s

### Iteration 2 (heuristic) — rejected
- `append_text` `instructions`: 'Whenever the caller mentions a day or time for an appointment, call check_availability for that date before proposing any time.' — agent proposed or skipped times without checking availability
- pass rate Δ -0.05; regressions: ['booking-api-fails', 'slot-taken']; improvements: none
- gate: no improvement over baseline (0.57 → 0.52); critical scenario regressions: ['booking-api-fails']; scenario regressions: ['booking-api-fails', 'slot-taken']

### Iteration 3 (heuristic) — rejected
- `append_text` `instructions`: 'For any question about services, prices, insurance or policies, call search_knowledge_base first and answer only from what it returns.' — factual questions answered without retrieval
- pass rate Δ -0.02; regressions: ['faq-price']; improvements: none
- gate: no improvement over baseline (0.57 → 0.55); scenario regressions: ['faq-price']
