---
name: log-analyzer
description: Diagnose why an optimization run failed or behaved oddly by reading its solver.log and termination status, then write a structured diagnosis into run_record.json. Use when a run is INFEASIBLE, hit the time limit, errored, or looks wrong, or as the diagnose step in the refine loop.
---

# Log Analyzer (Task 3)

Read one run's `solver.log` + `run_record.json`, decide the root cause, and
write a structured `Diagnosis` back into the record. You diagnose; you do not
change the config (that is the `refiner`).

## Operating rules

1. **Branch on error origin first** — it is already recorded in
   `execution.error_origin`:
   - `preflight` → missing input file, bad config, no Gurobi licence. The
     `solver.log` begins with `PREFLIGHT FAILED` or the model's preflight error.
     This is **not** a model-infeasibility; suggested tier is usually A/B (fix
     the config or inputs), never a policy relaxation.
   - `solver` / status `INFEASIBLE` → the constraints genuinely cannot be met.
     This is where you reason about *which* constraint binds.
   - `runtime` → a Julia exception; read the stack trace tail.

2. **For INFEASIBLE, find the binding constraint, not just the symptom.** Cross
   the config against the model: a `clean: clean` run with a `CO2_limit` below
   the island's physical floor is infeasible *because of the cap*, not the
   solver. Reason about CO2 cap vs available clean capacity, RE share vs
   resource, demand vs buildable supply. Cite evidence by log line
   (`"log:<n>"`).

3. **Suggest an intervention tier, honestly.** Set
   `suggested_intervention_tier` to the *smallest* tier that could resolve it:
   - A if numeric (time limit, gap, tolerances, scaling).
   - B if a sanctioned parameter (import price, storage cap, scenario choice).
   - C **only** if the genuine fix is relaxing a policy constraint (CO2 cap, RE
     floor, coal ban). Do not down-rank a policy fix to look auto-applyable —
     the refiner will block C anyway, and mislabelling it defeats the guardrail.

4. **Write the diagnosis into the record:**
   ```python
   from framework import RunRecord, Diagnosis
   rec = RunRecord.load(run_dir / "run_record.json")
   rec.log_diagnosis = Diagnosis(status=..., root_cause=..., evidence=[...],
                                 suggested_intervention_tier=..., confidence=...)
   rec.save(run_dir / "run_record.json")
   ```

## Output

State the root cause in one sentence, the evidence lines, and the suggested
tier with a one-line justification. If the run was OPTIMAL, say so and suggest
handing off to the output-analyzer instead.
