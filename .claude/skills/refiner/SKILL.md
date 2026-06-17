---
name: refiner
description: Propose and apply controlled, tier-bounded changes to an optimization config in response to a diagnosis, then hand the new config back to model-runner. Use as the refine step in the loop. Model-agnostic. Enforces the scientific-integrity guardrail — never silently relaxes a policy constraint.
---

# Refiner (Task 5) — the guardrailed core

Given a run's `Diagnosis`, propose changes and let the framework decide which may
be auto-applied. **You propose; `apply_refinements` enforces.** Never bypass it or
hand-edit a config to apply a Tier C change.

## The non-negotiable rule

Changes are classified by the active adapter's `intervention_spec()`:

- **Tier A** (auto-apply): numerics (e.g. MIP gap, tolerances, time limit).
  Change how the solver searches, not what it solves.
- **Tier B** (auto-apply + flag): sanctioned parameters / scenario within the
  enum.
- **Tier C** (BLOCKED — human sign-off): relaxing a policy constraint (carbon
  cap, RE floor, coal ban, etc.). An infeasible model "fixed" by quietly
  loosening a cap is a feasible-but-wrong result. The framework refuses to
  auto-apply these; so do you.

Unknown keys default to Tier C. Out-of-enum values are rejected outright. Which
keys map to which tier comes from the adapter — `get_adapter().describe_config()`
spells out the policy levers for the active model.

## Operating procedure

```python
from framework import get_adapter, RunRecord, ProposedChange, apply_refinements
adapter = get_adapter()
rec = RunRecord.load(run_dir / "run_record.json")
proposals = [ProposedChange("mipgap", None, 0.05)]    # your reasoned proposals
outcome = apply_refinements(rec, proposals, adapter.intervention_spec())
rec.save(run_dir / "run_record.json")                 # audit trail persisted
```

Then:
- `outcome.applied` → run `outcome.next_config` via `model-runner` with
  `parent_run = rec.config_hash`.
- `outcome.blocked` (Tier C) → **stop and ask the human.** Present the exact
  policy change, its rationale, and the scientific implication. Do not proceed
  until they approve; record their decision.
- `outcome.rejected` → you proposed an illegal value; re-propose.

## How to choose proposals

Map the diagnosis to the smallest sufficient change: time-limit/slow → raise
`mipgap` then time limit (A); numerical instability → tighten/scale (A);
infeasible because a *sanctioned parameter* is too tight → adjust it (B);
infeasible because a *policy target* is physically unattainable → the Tier C
case: surface the trade-off, never auto-relax.

## Output

Report each proposal's tier and disposition (applied / blocked / rejected), the
resulting next config, and — if anything was blocked — the explicit question for
the human. Never claim a run was "fixed" if the only real fix was Tier C.
