# Termination Criteria Reference

This reference explains the termination criteria used by the Supervisory Controller and the rationale for each threshold.

---

## 1. Termination Types

### TERMINATE_SUCCESS
The session ended with a valid, acceptable solution. Triggered by:

| Trigger | Condition | Notes |
|---|---|---|
| `REFINER_STOP` | Refiner Agent recommends STOP | Single-iteration optimal: first run is feasible and all checks pass |
| `CONVERGENCE_PLATEAU` | Last 3 improvements all < 0.1% | Marginal return — further iterations unlikely to yield meaningful improvement |
| `WITHIN_BOUND` | Best feasible solution within 2% of theoretical lower bound | Solution quality is demonstrably near-optimal |

### TERMINATE_FAILURE
The session ended without a satisfactory solution. Triggered by:

| Trigger | Condition | Notes |
|---|---|---|
| `ITERATION_BUDGET_EXHAUSTED` | `current_iteration >= max_iterations` | Budget consumed |
| `WALL_TIME_BUDGET_EXHAUSTED` | `elapsed_wall_time_s >= 0.85 × wall_time_budget_s` | Reserve 15% for post-processing |
| `INFINITE_RESTRUCTURE_LOOP` | ≥ 3 `TRIGGER_RESTRUCTURE` events without improvement | Structural problem requiring human review |
| `SOLVER_CRASH` | Solver process exits abnormally | Unrecoverable without external diagnosis |

---

## 2. Iteration Budget

**Default: 10 iterations per session.**

Rationale:
- Iterations 1–3: Initial run + up to 2 fix cycles (parameter relaxation)
- Iterations 4–6: Structural changes and re-run
- Iterations 7–10: Fine-tuning and convergence confirmation
- Beyond 10: Diminishing returns; likely a structural problem

**Adjusting the budget:**
- Tight deadline: reduce to 5–6 iterations with aggressive MIP gap (0.005)
- Research mode: increase to 15–20 iterations with tighter gap (0.0005)

---

## 3. Wall-Clock Budget

**Default: 7,200 seconds (2 hours) total.**

**Allocation recommendation:**
- Solver time: 70% of budget (≈ 5,040 s for 2h budget)
- Post-processing (analysis, memory write): 15% (≈ 1,080 s)
- Buffer / overhead: 15% (≈ 1,080 s)

**Per-iteration time limit:**
- Default: 600 s per solver call
- After first timeout: 900 s (1.5×)
- Hard cap: 3,600 s (1 hour) per single solver call

---

## 4. Convergence Plateau Detection

**Threshold: 0.1% improvement across 3 consecutive feasible iterations.**

Why 0.1%? This is calibrated for the energy system optimization context:
- Typical initial improvement across iterations: 5–15% per iteration
- Improvement between iteration 3 and 4: ~2–5%
- Improvement between iterations 8 and 9: ~0.5–1%
- When improvement drops below 0.1%, additional computation yields less than the cost of running another iteration (solver time, HPC allocation)
- This matches standard practice for iterative heuristics in power systems scheduling

**Formula:**
```
improvement[i] = |objective[i-1] - objective[i]| / objective[i-1]
plateau = all(improvement[-3:] < 0.001)
```

---

## 5. Stall Detection (During Solver Execution)

**Threshold: MIP gap improves < 0.01% over 5 consecutive 30-second polls (= 150 s of stall time).**

A stalled solver is not the same as a slow solver:
- **Slow**: gap is still decreasing, just slowly → allow more time
- **Stalled**: gap has plateaued completely → early stop, warm start next iteration

When stall is detected:
1. Issue early stop signal to solver
2. Extract best incumbent found so far
3. Route to Refiner Agent with `CONVERGENCE_SLOW` trigger
4. Refiner will add cutting planes (`CutPasses: 5`, `Cuts: 2`) and enable warm start

---

## 6. Restructure Limit

**Maximum: 3 `TRIGGER_RESTRUCTURE` events per session.**

Why 3? 
- 1st restructure: adds capacity or changes fleet composition (reasonable fix)
- 2nd restructure: verifies the first restructure resolved the problem
- 3rd restructure: if still failing, the problem is almost certainly structural and requires human judgment
- Beyond 3: risk of infinite loop; computational cost is not justified

After 3 restructures without a feasible solution: `TERMINATE_FAILURE` with a detailed diagnostic message for human review.

---

## 7. Single-Iteration Termination

The most common case in the demo/MVP:
1. Scenario Builder produces valid config ✓
2. Model Executor runs mock solver → `OPTIMAL` ✓
3. Solver Log Analyzer → `OK` ✓
4. Output Analysis Agent → `OK` ✓
5. Refiner Agent evaluates: first feasible optimal → `STOP` ✓
6. Supervisory Controller: `TERMINATE_SUCCESS` ✓

This single-iteration path terminates in 1 iteration because there is nothing to refine. This is correct behavior and the most efficient possible outcome.
