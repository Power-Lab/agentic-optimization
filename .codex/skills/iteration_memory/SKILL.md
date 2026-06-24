---
name: iteration_memory
description: "Use this skill to record, query, and summarize optimization iteration history so future runs avoid repeated failures and track convergence."
---

# Skill: Iteration Memory / History

## 1. Skill Name

**Iteration Memory / History**

---

## 2. Purpose

Maintains a persistent, structured record of all past optimization runs within a session and across sessions. Enables downstream skills to avoid repeating failed strategies, tracks convergence trends, supports run comparison, and provides context for the Supervisory Controller and Refiner Agent to make informed decisions.

---

## 3. When to Use

Invoke this skill:

- **Read (query)**: At the start of every new iteration, before the Supervisory Controller dispatches the Model Executor
- **Write (update)**: After every completed iteration (success or failure), after the Output Analysis Agent and Refiner Agent have finished
- **Query (pattern check)**: Before the Refiner Agent proposes a fix — check whether the same fix has been tried
- **Query (best solution)**: When the Supervisory Controller considers early termination

---

## 4. Inputs

### Write (update) input:

```yaml
memory_write_input:
  session_id: "session_20240101_001"
  run_id: "run_id_001"
  iteration: 1
  scenario_config_hash: "sha256:a3f1b2c9d4e8..."
  scenario_summary:
    generators: ["gas_cc_1", "gas_ct_1", "solar_1", "wind_1"]
    horizon_T: 24
    emission_cap_tCO2: 5000
    renewable_penetration_min: 0.60
  solver_result:
    status: "OPTIMAL"
    objective_value_USD: 163249.0
    mip_gap_final: 0.00082
    solve_time_s: 312.7
  log_analysis_summary:
    overall_status: "OK"
    infeasibility_detected: false
    numerical_issues_detected: false
    convergence_rate: "NORMAL"
  output_analysis_summary:
    overall_status: "OK"
    anomalies: []
  refiner_summary:
    fix_applied: null
    fix_type: null
    recommendation: "STOP"
  is_best_feasible: true
  timestamp: "2024-01-01T08:35:00Z"
```

### Read (query) input:

```yaml
memory_query_input:
  session_id: "session_20240101_001"
  run_id_current: "run_id_001"
  query_type: "FULL_HISTORY" # FULL_HISTORY | BEST_SOLUTION | FAILURE_PATTERNS | CONVERGENCE_TREND | RUN_COMPARISON
  comparison_run_ids: []
```

---

## 5. Outputs

### Read output:

```yaml
iteration_history:
  session_id: "session_20240101_001"
  total_iterations: 3
  best_run_id: "run_id_001"
  best_objective_value: 163249.0
  best_solution_path: "runs/run_id_001/dispatch.csv"
  convergence_trend:
    - run_id: "run_id_001"
      iteration: 1
      objective_value_USD: 163249.0
      status: "OPTIMAL"
  improvement_pct_last: null
  improvement_pct_total: null
  plateau_detected: false
  known_failure_patterns: []
  recommended_avoid: []
  run_log:
    - run_id: "run_id_001"
      iteration: 1
      timestamp: "2024-01-01T08:35:00Z"
      scenario_config_hash: "sha256:a3f1b2c9d4e8..."
      objective_value_USD: 163249.0
      solver_status: "OPTIMAL"
      log_status: "OK"
      output_status: "OK"
      fix_applied: null
```

### Run comparison output:

```yaml
run_comparison:
  runs:
    - run_id: "run_id_001"
      objective_value_USD: 182000.0
      total_emissions_tCO2: 3240.0
      renewable_fraction: 0.582
      solve_time_s: 445.2
      solver_status: "OPTIMAL"
    - run_id: "run_id_003"
      objective_value_USD: 163249.0
      total_emissions_tCO2: 2752.0
      renewable_fraction: 0.622
      solve_time_s: 312.7
      solver_status: "OPTIMAL"
  delta:
    objective_value_pct: -10.3
    emissions_pct: -15.1
    renewable_fraction_delta: +0.040
    solve_time_pct: -29.7
  best_run_id: "run_id_003"
  summary: "run_id_003 achieves 10.3% lower cost, 15.1% lower emissions, and higher renewable penetration vs run_id_001, with 30% faster solve time."
```

---

## 6. Step-by-Step Instructions

### Write procedure:

1. **Compute scenario config hash.**
   - Serialize `scenario_config` to canonical JSON (sorted keys, 6 decimal precision).
   - Compute SHA-256. Use as deduplication key.

2. **Append to `run_log`.**
   - Add entry from `memory_write_input`. Keep sorted by `iteration`.

3. **Update `convergence_trend`.**
   - If `status` is `OPTIMAL` or `TIME_LIMIT` and `objective_value_USD` is not null, append.
   - Recompute `improvement_pct_last` and `improvement_pct_total`.
   - **Plateau check**: if last 3 entries all improve by < 0.1%, set `plateau_detected = true`.

4. **Update `best_run_id`.**
   - If `is_best_feasible = true` OR `objective_value_USD < current best`, update `best_run_id`.

5. **Record failure patterns.**
   - If `infeasibility_detected = true` or `output_analysis == ANOMALY`:
     - `pattern_hash = SHA-256(scenario_config_hash + cause)`
     - If hash exists: update `outcome`. If new: create entry.
     - Derive `recommended_avoid` rule (e.g., if cap X caused infeasibility: `"emission_cap_tCO2 < X+100"`).

6. **Persist to storage.**
   - Write `run_history.jsonl` (one JSON line per run) for append-only logging.
   - Write `memory/session_{session_id}.json` for full structured state.
   - Write `memory/session_{session_id}_summary.yaml` for human readability.

### Read procedure:

7. **Load memory record.**
   - Load `memory/session_{session_id}.json`. If not found, return empty `iteration_history` (first run).

8. **Dispatch by `query_type`.**
   - `FULL_HISTORY`: return complete record.
   - `BEST_SOLUTION`: return only `best_run_id`, `best_objective_value`, `best_solution_path`.
   - `FAILURE_PATTERNS`: return `known_failure_patterns` and `recommended_avoid`.
   - `CONVERGENCE_TREND`: return `convergence_trend`, `improvement_pct_last`, `improvement_pct_total`, `plateau_detected`.
   - `RUN_COMPARISON`: compute delta between `comparison_run_ids`.

9. **Validate data freshness.**
   - Check that most recent record timestamp is within expected window. If gap detected, flag `WARN: MEMORY_INCONSISTENCY`.

---

## 7. Heuristics / Rules

- **Plateau threshold**: 0.1% improvement across 3 consecutive feasible runs. Signal to Supervisory Controller to consider `TERMINATE_SUCCESS`.
- **Pattern deduplication**: `scenario_config_hash + cause` as composite key. A different emission cap but same fleet topology hashes differently (correct behavior).
- **Failure escalation**: Same pattern fails 3 times with different fixes → add `STRUCTURAL_ISSUE_FLAGGED`. Signal Refiner to stop parameter fixes and trigger `RESTRUCTURE`.
- **Comparison ranking**: Primary sort `objective_value_USD`. If within 0.5%, prefer lower `total_emissions_tCO2`, then lower `solve_time_s`.
- **Retention policy**: All runs within a session. Across sessions: top-5 best feasible + all failure patterns indefinitely. Prune intermediate runs older than 7 days.
- **`recommended_avoid` is advisory**: Refiner Agent may override with explicit justification. Prevents over-conservatism.

---

## 8. Failure Modes

| Failure                    | Condition                                | Response                                                                      |
| -------------------------- | ---------------------------------------- | ----------------------------------------------------------------------------- |
| `MEMORY_FILE_NOT_FOUND`    | Session JSON doesn't exist on first read | Return empty `iteration_history`. Initialize on first write.                  |
| `MEMORY_WRITE_FAILED`      | File system error during write           | Retry once. If still failing, continue with in-memory state.                  |
| `CORRUPTED_MEMORY_FILE`    | JSON parse error on load                 | Load last valid backup. If none, initialize fresh. Flag `WARN: MEMORY_RESET`. |
| `HASH_COLLISION`           | Two configs produce the same hash        | Append run ID suffix to key. Log collision.                                   |
| `MEMORY_INCONSISTENCY`     | Gap in iteration sequence                | Flag `WARN`. Log missing iterations. Do not block downstream.                 |
| `COMPARISON_RUN_NOT_FOUND` | Requested run ID has no record           | Return partial comparison with `null` for missing run.                        |

---

## 9. Example

**Write input (infeasible iteration 2):**

```yaml
memory_write_input:
  session_id: "session_20240101_001"
  run_id: "run_id_002"
  iteration: 2
  scenario_config_hash: "sha256:deadbeef1234..."
  scenario_summary:
    emission_cap_tCO2: 800
    renewable_penetration_min: 0.70
  solver_result:
    status: "INFEASIBLE"
    objective_value_USD: null
  log_analysis_summary:
    overall_status: "INFEASIBLE"
    infeasibility_detected: true
    cause: "EMISSION_CAP_TOO_TIGHT"
    iis_constraints: ["emission_cap[total]", "renewable_penetration_minimum"]
  refiner_summary:
    fix_applied: "emission_cap_tCO2: 800 → 1000"
    recommendation: "CONTINUE"
  is_best_feasible: false
```

**Resulting `known_failure_patterns` entry:**

```yaml
known_failure_patterns:
  - pattern_hash: "abc123"
    scenario_config_hash: "sha256:deadbeef1234..."
    cause: "EMISSION_CAP_TOO_TIGHT"
    iis_constraints: ["emission_cap[total]", "renewable_penetration_minimum"]
    fix_attempted: "emission_cap_tCO2: 800 → 1000"
    outcome: "PENDING"
    iteration: 2
recommended_avoid:
  - "emission_cap_tCO2 < 900"
```
