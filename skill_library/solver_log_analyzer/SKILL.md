---
name: solver_log_analyzer
description: "Use this skill to parse solver logs and diagnose infeasibility, convergence failures, timeouts, and numerical instability."
---

# Skill: Solver Log Analyzer

## 1. Skill Name
**Solver Log Analyzer**

---

## 2. Purpose
Parses raw solver log output from Gurobi (or compatible solvers) to detect infeasibility, convergence problems, and numerical instability. Produces a structured diagnostic report consumed by the Supervisory Controller and Refiner Agent to determine the next action.

---

## 3. When to Use
Invoke this skill:
- Immediately after every Model Executor run, regardless of termination status
- When `executor_output.status` is `COMPLETED`, `TIMEOUT`, `FAILED`, or `INFEASIBLE`
- When the Supervisory Controller requests mid-run log analysis due to solver stalling

---

## 4. Inputs

```yaml
log_analyzer_input:
  run_id: "run_id_001"
  log_file_path: "runs/run_id_001/solver.log"
  executor_output:
    status: "COMPLETED"
    solver_termination_status: "OPTIMAL"
    objective_value_USD: 163249.0
    mip_gap_final: 0.00082
    solve_time_s: 312.7
  scenario_config:
    generators: [ ... ]
    policy_constraints:
      emission_cap_tCO2: 5000
      renewable_penetration_min: 0.60
  prior_run_diagnostics: null          # Output of this skill from previous iteration; null on first run
```

---

## 5. Outputs

```yaml
log_analysis_report:
  run_id: "run_id_001"
  overall_status: "OK"                 # OK | WARN | INFEASIBLE | NUMERICAL_ERROR | TIMEOUT_SUBOPTIMAL
  infeasibility:
    detected: false
    type: null                         # PRIMAL_INFEASIBLE | DUAL_INFEASIBLE | INFEASIBLE_OR_UNBOUNDED | null
    iis_constraints: []
    likely_cause: null
  convergence:
    converged: true
    final_mip_gap: 0.00082
    gap_trajectory: [0.152, 0.048, 0.018, 0.005, 0.00082]
    convergence_rate: "NORMAL"         # FAST | NORMAL | SLOW | STALLED | DIVERGING
    bound_quality: "TIGHT"             # TIGHT | MODERATE | LOOSE
    presolve_reductions:
      rows_removed: 480
      columns_removed: 320
  numerical_issues:
    detected: false
    patterns: []
    scaling_warnings: false
    coefficient_range:
      min: 1.0
      max: 600.0
      ratio: 600.0
      status: "ACCEPTABLE"            # ACCEPTABLE | WARN | CRITICAL
  performance:
    node_count: 4821
    nodes_per_second: 15.4
    lp_relaxation_value_USD: 161900.0
    integrality_gap_pct: 0.83
    presolve_time_s: 3.1
    lp_solve_time_s: 28.4
    mip_solve_time_s: 281.2
  warnings: []
  errors: []
  recommendations:
    - "Solution is optimal and numerically clean. Proceed to output analysis."
```

---

## 6. Step-by-Step Instructions

1. **Load and preprocess the log file.**
   - Read raw log line by line. Strip ANSI codes, normalize whitespace.
   - Identify solver version block at the top.
   - Locate key sections: Presolve, LP Relaxation, MIP Tree, Solution, Termination Message.

2. **Check termination status.**
   - Scan for Gurobi termination strings:
     - `"Optimal solution found"` → `OPTIMAL`
     - `"Model is infeasible"` → `INFEASIBLE`
     - `"Model is infeasible or unbounded"` → `INFEASIBLE_OR_UNBOUNDED`
     - `"Time limit reached"` → `TIME_LIMIT`
     - `"Numeric focus required"` → `NUMERICAL_ERROR`
   - If `OPTIMAL` or `TIME_LIMIT`: continue to convergence analysis.
   - Otherwise: jump to infeasibility analysis.

3. **Infeasibility analysis.**
   - Search for IIS constraint list in log (e.g., `"IIS computed: ..."` line).
   - Map IIS constraint IDs to model constraint types:
     - `emission_cap[total]` + `renewable_penetration_minimum` → `EMISSION_CAP_TOO_TIGHT`
     - `power_balance[t=*]` for > 30% of timesteps → `DEMAND_SUPPLY_MISMATCH`
     - `ramp[g,t]` + `commitment[g,t]` → `COMMITMENT_RAMP_INFEASIBILITY`
   - Populate `infeasibility.iis_constraints` and `infeasibility.likely_cause`.

4. **Convergence analysis.**
   - Extract MIP progress table: parse `Nodes`, `Best Bound`, `Best Incumbent`, `Gap` columns.
   - Build `gap_trajectory` (subsample to 5–10 points if > 100 rows).
   - Classify `convergence_rate`:
     - `FAST`: gap < 1% in first 20% of solve time
     - `NORMAL`: steady decline
     - `SLOW`: gap > 1% for > 80% of solve time
     - `STALLED`: gap changes < 0.01% across 5 consecutive rows
     - `DIVERGING`: gap increases at any point (always `ERROR`)
   - Classify `bound_quality`:
     - `TIGHT`: LP relaxation within 2% of final MIP objective
     - `MODERATE`: 2–10%
     - `LOOSE`: > 10%

5. **Numerical instability analysis.**
   - Search for warning strings:
     - `"Numeric values are large"` → `LARGE_COEFFICIENTS`
     - `"Warning: max constraint coefficient"` → `COEFFICIENT_RANGE_WARNING`
     - `"Dual objective and primal objective disagree"` → `PRIMAL_DUAL_DISAGREEMENT`
     - `"Solutions are not improving"` → `INCUMBENT_STAGNATION`
     - `"Presolve removed 0 rows and 0 columns"` → `NO_PRESOLVE_REDUCTION`
   - Compute coefficient range ratio from `scenario_config`. Flag if ratio > 1e6.

6. **Extract performance metrics.**
   - Parse: node count, LP relaxation objective, presolve stats, wall/solve times.
   - Compute `integrality_gap_pct = (obj - lp_relaxation) / lp_relaxation * 100`.

7. **Generate recommendations.**
   - Append one recommendation string per issue. Set `overall_status` from highest severity.

---

## 7. Heuristics / Rules

- **Infeasibility → emission cap**: If IIS contains `emission_cap[total]`, recommend relaxing cap by 20%.
- **Infeasibility → power balance**: If IIS touches > 30% of time steps, recommend adding capacity (`RESTRUCTURE`).
- **Stalled gap** (< 0.01% improvement over 5 rows, > 50% of time budget): recommend early stop + warm start next iteration.
- **DIVERGING gap**: always `ERROR`. Do not continue — numerical issue, not a modeling one.
- **LP gap > 10%**: MIP may be fundamentally hard. Recommend relaxing binary variables (min up/down time).
- **PRIMAL_DUAL_DISAGREEMENT**: always `ERROR`. Solution may be incorrect even if reported as optimal.
- **Nodes/second < 1.0**: LP per node is expensive. Recommend reducing model resolution.
- **integrality_gap_pct > 5**: LP bound is weak. Recommend cutting planes.
- **NO_PRESOLVE_REDUCTION on > 1000-variable model**: suspicious. Recommend constraint audit.

---

## 8. Failure Modes

| Failure | Condition | Response |
|---|---|---|
| `LOG_FILE_NOT_FOUND` | `log_file_path` does not exist | `overall_status = NUMERICAL_ERROR`. Attempt fallback from `executor_output` fields only. |
| `LOG_PARSE_ERROR` | Log truncated or corrupted | Parse available content. Flag `WARN: PARTIAL_LOG`. |
| `IIS_COMPUTATION_FAILED` | IIS not computable | Report infeasibility without constraint-level detail. Recommend manual inspection. |
| `UNEXPECTED_TERMINATION` | Log ends without standard termination message | Treat as `NUMERICAL_ERROR`. Check HPC walltime and memory limits. |
| `DIVERGING_GAP` | Bound worsens during solve | `ERROR`. Recommend immediate termination and formulation review. |

---

## 9. Example

**Input (infeasible run):**
```yaml
log_analyzer_input:
  run_id: "run_id_002"
  log_file_path: "runs/run_id_002/solver.log"
  executor_output:
    status: "INFEASIBLE"
    solver_termination_status: "INFEASIBLE"
    objective_value_USD: null
    solve_time_s: 12.4
  scenario_config:
    policy_constraints:
      emission_cap_tCO2: 800
      renewable_penetration_min: 0.70
```

**Output:**
```yaml
log_analysis_report:
  run_id: "run_id_002"
  overall_status: "INFEASIBLE"
  infeasibility:
    detected: true
    type: "PRIMAL_INFEASIBLE"
    iis_constraints:
      - "emission_cap[total]"
      - "power_balance[t=08]"
      - "power_balance[t=09]"
      - "renewable_penetration_minimum"
    likely_cause: "EMISSION_CAP_TOO_TIGHT"
  convergence:
    converged: false
    final_mip_gap: null
    gap_trajectory: []
    convergence_rate: "N/A"
    bound_quality: "N/A"
  numerical_issues:
    detected: false
    patterns: []
  recommendations:
    - "Emission cap of 800 tCO2 is infeasible given the available renewable capacity. Recommend relaxing to at least 1100 tCO2 or adding storage/dispatchable capacity."
    - "Consider reducing renewable_penetration_min from 0.70 to 0.60 as an interim fix."
```
