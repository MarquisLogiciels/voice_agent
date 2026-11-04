# Optimization report

- Decision: **no_improvement** (production promotion still requires human approval)
- Baseline pass rate: 62% (95% CI 47%–75%) over 42 runs

## Baseline failure categories

- tool: 17
- state: 16
- mentions: 3
- simulation_completed: 2
- never_mentions: 2
- no_false_success_claims: 1
- turn1_reply: 1

## Iterations

### Iteration 1 (heuristic) — rejected
- `append_text` `instructions`: 'Once the caller agrees to a specific slot and you have their name and the reason for the visit, read the details back, get a clear yes, then call book_appointme' — bookings were not completed
- pass rate Δ +0.00; regressions: ['slot-taken']; improvements: ['faq-price']
- gate: no improvement over baseline (0.62 → 0.62); scenario regressions: ['slot-taken']
