---
name: supervisory_controller
description: "Use this skill to orchestrate iterative energy system optimization runs, route workflow state, and enforce solver iteration budgets."
---

# Skill: Supervisory Controller

## 1. Skill Name
**Supervisory Controller**

---

## 2. Purpose
Orchestrates the full iterative optimization loop. Manages workflow state, monitors real-time solver progress, enforces iteration and wall-clock budgets, and routes outputs to the correct downstream skill at each iteration. Acts as the single source of truth for session state.

---

## 3. When to Use
Invoke this skill:
- Immediately after the Scenario Builder Agent produces a valid `scenario_config` (iteration 0 start)
- At the start of every subsequent iteration, before launching the Model Executor
- Continuously during solver execution to monitor live progress
- When any downstream skill returns a non-OK status (log analyzer, output analyzer, refiner)

---

## 4. Inputs

```yaml
controller_input:
  run_id: "run_id_001"
  scenario_config: { ... }             # Full output of Scenario Builder Agent
  iteration_state:
    current_iteration: 0               # 0-indexed; 0 on first call
    max_iterations: 10
    elapsed_wall_time_s: 0
    wall_time_budget_s: 7200           # 2 hours total
    restructure_count: 0               # Number of TRIGGER_RESTRUCTURE actions so far
  iteration_history:                   # Output of Iteration Memory; null on first call
    best_run_id: null
    best_objective_value: null
    convergence_trend: []
    plateau_detected: false
    known_failure_patterns: []
  solver_monitor:                      # Live feed; null before solver starts
    status: null                       # RUNNING | COMPLETED | TIMEOUT | CRASHED
    last_mip_gap: null
    last_bound: null
    last_incumbent: null
    elapsed_solver_time_s: null
  downstream_status:                   # Statuses from downstream skills; null on first call
    solver_log_analysis: null          # OK | WARN | INFEASIBLE | NUMERICAL_ERROR | TIMEOUT_SUBOPTIMAL
    output_analysis: null              # OK | WARN | ANOMALY
    refiner_recommendation: null       # CONTINUE | STOP | RESTRUCTURE
```

---

## 5. Outputs

```yaml
controller_decision:
  run_id: "run_id_001"
  iteration: 1
  action: "LAUNCH_SOLVER"             # LAUNCH_SOLVER | WAIT | TERMINATE_SUCCESS | TERMINATE_FAILURE | TRIGGER_REFINER | TRIGGER_RESTRUCTURE
  reason: "First iteration — launching solver with base scenario config."
  next_skill: "model_executor"        # model_executor | refiner_agent | scenario_builder | null
  updated_solver_hints:
    mip_gap: 0.001
    time_limit_s: 600
    warm_start: false
  iteration_budget_remaining: 9
  wall_time_remaining_s: 7200
  termination_criteria_met: false
  log_entry:
    timestamp: "2024-01-01T08:01:00Z"
    event: "ITERATION_START"
    details: "Iteration 1 of 10. Dispatching to Model Executor."
```

---

## 6. Step-by-Step Instructions

### Pre-Solver Phase

1. **Check hard budget limits.**
   - If `current_iteration >= max_iterations`: `action = TERMINATE_FAILURE`, `reason = ITERATION_BUDGET_EXHAUSTED`. Stop.
   - If `elapsed_wall_time_s >= wall_time_budget_s * 0.85`: `action = TERMINATE_FAILURE`, `reason = WALL_TIME_BUDGET_EXHAUSTED`. (Reserve 15% for post-processing.) Stop.
   - If `restructure_count >= 3`: `action = TERMINATE_FAILURE`, `reason = INFINITE_RESTRUCTURE_LOOP`. Stop.

2. **Check for known-infeasible scenario fingerprints.**
   - Query `iteration_history.known_failure_patterns`. If the current `scenario_config_hash` matches a prior FAILED pattern where all fixes were exhausted, set `action = TRIGGER_RESTRUCTURE`.

3. **Evaluate convergence plateau.**
   - If `iteration_history.plateau_detected = true` OR the last 3 entries in `convergence_trend` all improve by < 0.1%: set `action = TERMINATE_SUCCESS`, `reason = CONVERGENCE_PLATEAU`.

4. **Set solver hints for this iteration.**
   - If prior run timed out: `time_limit_s = min(prior * 1.5, 3600)`.
   - If prior `mip_gap_final > 0.01`: `mip_gap = current * 0.5`.
   - If Refiner Agent suggested warm start: `warm_start = true`.

5. **Dispatch to Model Executor.**
   - `action = LAUNCH_SOLVER`, `next_skill = model_executor`.
   - Log `ITERATION_START` event.

### During-Solver Monitoring Phase

6. **Poll every 30 seconds.**
   - Compute `gap_improvement_rate = (gap_at_start - last_mip_gap) / elapsed_solver_time_s`.
   - If `gap_improvement_rate < 0.0001` for 5 consecutive polls: issue early-stop signal (`SOLVER_STALLED`), route to Refiner Agent.
   - If `solver_monitor.status == CRASHED`: `action = TERMINATE_FAILURE`.

7. **Detect bound divergence.**
   - If `last_incumbent - last_bound < 0` (bound exceeds incumbent): `action = TERMINATE_FAILURE`, `reason = INVALID_BOUNDS`.
   - If bound worsening while incumbent improving: log `WARN: BOUND_DIVERGENCE`, continue 2 more polls.

### Post-Solver Phase

8. **Route by downstream status (priority order):**
   - `solver_log_analysis == INFEASIBLE` → `TRIGGER_REFINER` with `INFEASIBILITY` context.
   - `solver_log_analysis == NUMERICAL_ERROR` → `TRIGGER_REFINER` with `NUMERICAL_INSTABILITY` context.
   - `output_analysis == ANOMALY` → `TRIGGER_REFINER` with `OUTPUT_ANOMALY` context.
   - `refiner_recommendation == STOP` → `TERMINATE_SUCCESS`.
   - `refiner_recommendation == RESTRUCTURE` → `TRIGGER_RESTRUCTURE`; increment `restructure_count`.
   - `refiner_recommendation == CONTINUE` → increment iteration, return to step 1.
   - All `OK`: evaluate plateau (step 3); else `LAUNCH_SOLVER` for next iteration.

9. **Update iteration state.**
   - Increment `current_iteration`. Update `elapsed_wall_time_s`.
   - Emit `controller_decision` with updated budget fields.

---

## 7. Heuristics / Rules

- **Stall threshold**: MIP gap improvement < 0.01% over 5 × 30-second polls (150 s) → stalled.
- **Diminishing returns**: < 0.1% objective improvement across 3 consecutive feasible iterations → plateau.
- **Restructure limit**: ≤ 3 `TRIGGER_RESTRUCTURE` actions per session. Beyond this: `TERMINATE_FAILURE`.
- **Warm start eligibility**: Only if prior iteration was feasible AND Refiner made parameter-only changes (not structural).
- **Time budget split**: Reserve 15% of `wall_time_budget_s` for post-processing. Never allocate all time to the solver.
- **Iteration 1 always runs**: Never terminate before the first solver call, even if the scenario looks marginal.

---

## 8. Failure Modes

| Failure | Condition | Response |
|---|---|---|
| `ITERATION_BUDGET_EXHAUSTED` | `current_iteration >= max_iterations` | Terminate. Return best feasible solution found so far. |
| `WALL_TIME_BUDGET_EXHAUSTED` | Elapsed ≥ 85% of budget | Terminate immediately. Return best incumbent. |
| `SOLVER_CRASH` | Process exits non-zero | Terminate iteration. Attempt one restart with reduced problem. |
| `SOLVER_STALLED` | Gap not improving for > 150 s | Early stop. Route to Refiner Agent with `CONVERGENCE_SLOW`. |
| `BOUND_DIVERGENCE` | Lower bound worsening while incumbent improves | WARN, continue 2 polls; if persists, terminate. |
| `INFINITE_RESTRUCTURE_LOOP` | ≥ 3 restructures without improvement | Force `TERMINATE_FAILURE`. Escalate to human review. |

---

## 9. Example

**Input (start of iteration 3, prior WARN was non-blocking):**
```yaml
controller_input:
  run_id: "run_id_003"
  iteration_state:
    current_iteration: 2
    max_iterations: 10
    elapsed_wall_time_s: 1240
    wall_time_budget_s: 7200
    restructure_count: 0
  iteration_history:
    best_run_id: "run_id_002"
    best_objective_value: 163249.0
    convergence_trend:
      - {run_id: "run_id_001", iteration: 1, objective_value_USD: 182000.0, status: "OPTIMAL"}
      - {run_id: "run_id_002", iteration: 2, objective_value_USD: 163249.0, status: "OPTIMAL"}
    plateau_detected: false
  downstream_status:
    solver_log_analysis: "OK"
    output_analysis: "WARN"
    refiner_recommendation: "CONTINUE"
```

**Output:**
```yaml
controller_decision:
  run_id: "run_id_003"
  iteration: 3
  action: "LAUNCH_SOLVER"
  reason: "Iteration 2 complete. Output WARN noted but non-blocking. Refiner recommends CONTINUE."
  next_skill: "model_executor"
  updated_solver_hints:
    mip_gap: 0.001
    time_limit_s: 600
    warm_start: true
  iteration_budget_remaining: 7
  wall_time_remaining_s: 5960
  termination_criteria_met: false
  log_entry:
    timestamp: "2024-01-01T08:42:00Z"
    event: "ITERATION_START"
    details: "Iteration 3 of 10. Warm start enabled. 10.4% improvement last iteration."
```
