---
name: supervisor
description: Run the full closed iterate loop — build/run a scenario, diagnose, refine, re-run — until it solves, gets stuck, or hits a Tier-C policy block that needs a human. Keeps an iteration ledger, never repeats a tried config, and enforces the guardrail. Use when the user wants a scenario driven to a satisfactory result autonomously ("get this to solve", "iterate until it converges"). Model-agnostic.
---

# Supervisor (Task 6) — the closed loop

Orchestrate `model-runner` → `log-analyzer`/`output-analyzer` → `refiner` →
`model-runner` … until a stopping condition. The loop machinery, dedup, and
guardrail enforcement live in `framework.supervisor.Supervisor`; you supply the
*reasoning* (the `propose_fn`) by acting as the refiner each iteration.

## How to drive it

```python
from framework import get_adapter, Supervisor, StopCriteria, ProposedChange
adapter = get_adapter()
sup = Supervisor(adapter)

def propose(record):
    # YOU reason here, as the log/output-analyzer + refiner would:
    # inspect record.execution / record.log_diagnosis and return the smallest
    # sufficient ProposedChange list. Return [] if nothing safe is left to try.
    ...

result = sup.run(config, propose, run_root="runs/<goal>", stop=StopCriteria(max_iters=5))
```

The supervisor enforces, so the loop cannot misbehave:
- **stops on success** when `termination_status == target` (default OPTIMAL),
- **refuses to repeat** a `config_hash` it has already run (no infinite loops),
- **halts for a human** the moment a proposal is Tier C (policy relaxation) — it
  never launders a cap/floor change through the loop,
- **halts** when you propose nothing (`stuck`) or hit `max_iters` (`exhausted`).

## Operating rules

1. Each iteration, treat `record` exactly as the analyzer + refiner skills would:
   diagnose first, then propose the *smallest* tier-appropriate change.
2. Never propose a Tier-C change just to keep the loop going. If the only fix is
   policy relaxation, let the supervisor stop with `needs_human` and present the
   trade-off — that outcome is correct, not a failure.
3. Report the `SupervisorResult`: outcome, iteration count, and the ledger
   (each run's status + what was changed). The ledger is the audit trail.

## Output

A short narrative of the loop: how many iterations, what changed at each step,
and the final outcome — `solved`, `needs_human` (with the trade-off), `stuck`,
`cycle`, or `exhausted`.
