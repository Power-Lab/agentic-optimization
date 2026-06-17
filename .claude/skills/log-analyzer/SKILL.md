---
name: log-analyzer
description: Diagnose why an optimization run failed or behaved oddly by reading its solver.log and termination status, then write a structured diagnosis into run_record.json. Use when a run is INFEASIBLE, hit the time limit, errored, or looks wrong, or as the diagnose step in the refine loop. Model-agnostic.
---

# Log Analyzer (Task 3)

Read one run's `solver.log` + `run_record.json`, decide the root cause, and write
a structured `Diagnosis` back into the record. You diagnose; you do not change
the config (that is the `refiner`).

## Operating rules

1. **Branch on error origin first** — it is recorded in `execution.error_origin`:
   - `preflight` → bad config / missing input / licence problem. The `solver.log`
     begins with `PREFLIGHT FAILED` or the model's preflight error. This is not a
     model-infeasibility; the fix is usually a numeric/parameter change, never a
     policy relaxation.
   - `solver` / status `INFEASIBLE` → the constraints genuinely cannot be met.
     Reason about *which* constraint binds.
   - `runtime` → an exception; read the trace tail.

2. **For INFEASIBLE, find the binding constraint, not just the symptom.** Cross
   the config against the model's semantics (`get_adapter().describe_config()`):
   which policy cap, share floor, or capacity limit makes it unsatisfiable? Cite
   evidence by log line (`"log:<n>"`).

3. **Suggest the smallest sufficient intervention tier, honestly.** Set
   `suggested_intervention_tier` to A (numeric), B (sanctioned parameter), or C
   (only if the genuine fix relaxes a policy constraint). Do not down-rank a
   policy fix to look auto-applyable — the refiner blocks C regardless, and
   mislabelling defeats the guardrail.

4. **Write the diagnosis into the record:**
   ```python
   from framework import RunRecord, Diagnosis
   rec = RunRecord.load(run_dir / "run_record.json")
   rec.log_diagnosis = Diagnosis(status=..., root_cause=..., evidence=[...],
                                 suggested_intervention_tier=..., confidence=...)
   rec.save(run_dir / "run_record.json")
   ```

## Output

State the root cause in one sentence, the evidence lines, and the suggested tier
with a one-line justification. If the run was OPTIMAL, say so and suggest handing
off to `output-analyzer`.
