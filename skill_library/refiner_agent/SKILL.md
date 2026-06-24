---
name: refiner_agent
description: "Use this skill to convert solver diagnostics and output anomalies into targeted scenario refinements for the next optimization iteration."
---

# Skill: Refiner Agent

## 1. Skill Name
**Refiner Agent**

---

## 2. Purpose
Diagnoses the root cause of solver failures, output anomalies, or convergence problems and proposes targeted fixes. Translates diagnostic reports from the Solver Log Analyzer and Output Analysis Agent into concrete parameter modifications, configuration adjustments, or structural changes to the scenario. Feeds the Scenario Builder Agent with refinement overrides for the next iteration.

---

## 3. When to Use
Invoke this skill:
- When the Supervisory Controller triggers `action = TRIGGER_REFINER`
- After any iteration where `solver_log_analysis != OK` or `output_analysis != OK`
- When the Solver Log Analyzer reports `INFEASIBLE`, `NUMERICAL_ERROR`, or `TIMEOUT_SUBOPTIMAL`
- When the Output Analysis Agent reports `ANOMALY` or `WARN` on dispatch, cost, or emission checks

---

## 4. Inputs

```yaml
refiner_input:
  run_id: "run_id_002"
  trigger_context: "INFEASIBILITY"     # INFEASIBILITY | NUMERICAL_INSTABILITY | OUTPUT_ANOMALY | TIMEOUT | CONVERGENCE_SLOW
  log_analysis_report:
    overall_status: "INFEASIBLE"
    infeasibility:
      detected: true
      type: "PRIMAL_INFEASIBLE"
      iis_constraints:
        - "emission_cap[total]"
        - "power_balance[t=08]"
        - "renewable_penetration_minimum"
      likely_cause: "EMISSION_CAP_TOO_TIGHT"
    convergence:
      converged: false
    numerical_issues:
      detected: false
  output_analysis_report: null         # null if no solution exists
  scenario_config:
    policy_constraints:
      emission_cap_tCO2: 800
      renewable_penetration_min: 0.70
    solver_settings:
      mip_gap: 0.001
      time_limit_s: 600
  iteration_history:
    best_run_id: null
    best_objective_value: null
    convergence_trend: []
    plateau_detected: false
    known_failure_patterns:
      - pattern_hash: "abc123"
        cause: "EMISSION_CAP_TOO_TIGHT"
        fix_attempted: "emission_cap_tCO2: 800 → 1000"
        outcome: "STILL_INFEASIBLE"
```

---

## 5. Outputs

```yaml
refiner_output:
  run_id: "run_id_002"
  recommendation: "CONTINUE"          # CONTINUE | STOP | RESTRUCTURE
  fix_type: "PARAMETER_RELAXATION"    # PARAMETER_RELAXATION | SOLVER_TUNING | MODEL_REFORMULATION | STRUCTURAL_CHANGE | NO_FIX
  confidence: "HIGH"                  # HIGH | MEDIUM | LOW
  refinement_overrides:
    policy_constraints:
      emission_cap_tCO2: 1200          # relaxed from 800 (prior fix to 1000 failed — escalating)
      renewable_penetration_min: 0.60  # relaxed from 0.70
    solver_settings:
      mip_gap: 0.001
      time_limit_s: 600
      warm_start: false
  structural_change_request: null
  explanation: "Emission cap of 800 tCO2 is infeasible. Prior relaxation to 1000 tCO2 also failed. Escalating to 1200 tCO2 and reducing renewable penetration target from 0.70 to 0.60, which is the minimum achievable given the current fleet during morning and evening thermal-dominant hours."
  fix_history_entry:
    iteration: 2
    trigger: "INFEASIBILITY"
    cause: "EMISSION_CAP_TOO_TIGHT"
    fix_applied: "emission_cap_tCO2: 800 → 1200, renewable_penetration_min: 0.70 → 0.60"
    prior_attempts: 1
```

---

## 6. Step-by-Step Instructions

1. **Identify the primary failure mode.**
   - Read `trigger_context`. Cross-reference `log_analysis_report.overall_status` and `output_analysis_report.overall_status`.
   - Priority: `INFEASIBLE > NUMERICAL_ERROR > ANOMALY > TIMEOUT > WARN`.

2. **Check Iteration Memory for prior fix attempts.**
   - Query `iteration_history.known_failure_patterns` for matching `cause`.
   - If prior fix failed: escalate magnitude (2× relaxation or different approach).
   - If same cause failed 3+ times: `recommendation = RESTRUCTURE`.

3. **Apply fix logic by failure mode.**

   **INFEASIBILITY:**
   - `emission_cap[total]` in IIS → relax `emission_cap_tCO2` by 20% (1st attempt), 40% (2nd attempt).
   - `renewable_penetration_minimum` in IIS → reduce `renewable_penetration_min` by 0.05 per attempt.
   - `power_balance[t=*]` > 30% of timesteps → `RESTRUCTURE` (insufficient capacity — add generators).
   - `ramp[g,t]` → increase ramp rate by 10% per attempt.
   - `min_up_time[g,t]` / `min_down_time[g,t]` → reduce by 1 hour per attempt.

   **NUMERICAL_INSTABILITY:**
   - `LARGE_COEFFICIENTS` → add `NumericFocus: 3`, `ScaleFlag: 2` to solver hints.
   - `PRIMAL_DUAL_DISAGREEMENT` → add `NumericFocus: 3`, `ObjScale: -0.5`. Treat as `ERROR`.
   - Coefficient ratio > 1e6 → `MODEL_REFORMULATION`: normalize units (MW → GW for large capacities).

   **OUTPUT_ANOMALY:**
   - `RAMP_VIOLATION` → add `ramp_constraint_debug: true` to solver hints. Verify ramp constraint was applied.
   - `BELOW_PMIN` → re-emit corrected generator config with validated `P_min_MW`.
   - `POWER_BALANCE_VIOLATION` → `RESTRUCTURE` immediately. Model construction error.
   - `RENEWABLE_TARGET_MISSED` → relax curtailment penalty in objective.

   **TIMEOUT:**
   - 1st attempt: `time_limit_s *= 1.5`. Enable warm start if prior solution exists.
   - 2nd attempt: `mip_gap *= 2` (accept lower quality solution).
   - 3rd attempt: `binary_commitment = false` (LP relaxation).

   **CONVERGENCE_SLOW:**
   - Add `CutPasses: 5` to solver hints.
   - LP gap > 10%: add `Cuts: 2` (aggressive).
   - Integrality gap > 5%: add branching priority hint for commitment variables.

4. **Evaluate recommendation.**
   - Credible fix exists + budget remains → `CONTINUE`.
   - Physical infeasibility (peak demand > total capacity) → `RESTRUCTURE`.
   - Best feasible solution within 2% of theoretical lower bound → `STOP`.

5. **Compute confidence.**
   - `HIGH`: IIS maps directly to a known fixable constraint.
   - `MEDIUM`: Multiple possible causes; fix is a best guess.
   - `LOW`: Root cause unclear; speculative. Add debug flags.

6. **Emit `refiner_output`.**
   - Populate `refinement_overrides` as a sparse patch (Scenario Builder merges over base config).
   - Populate `fix_history_entry` for Iteration Memory.

---

## 7. Heuristics / Rules

- **Targeted fix first**: Never relax multiple constraints simultaneously on the first attempt. Isolate the cause.
- **Escalation rule**: Same constraint class infeasible across 2 consecutive iterations → escalate fix by 2×. After 3 failures → `RESTRUCTURE`.
- **Emission cap floor**: Never relax `emission_cap_tCO2` below `sum_t max(0, demand[t] - sum_VRE_max[t]) * min_thermal_emission_rate`. If floor hit → `RESTRUCTURE`.
- **Warm start eligibility**: Only if prior solution was feasible AND Refiner made parameter-only changes.
- **LP relaxation as diagnostic**: If root cause is unclear, recommend one LP relaxation run to isolate structural vs. integer infeasibility.
- **Never over-tighten**: Do not tighten any constraint unless output analysis shows > 20% slack.
- **WARN trigger → STOP, not RESTRUCTURE**: A `WARN` from output analysis that doesn't worsen across iterations should trigger `STOP`, not `RESTRUCTURE`.

---

## 8. Failure Modes

| Failure | Condition | Response |
|---|---|---|
| `NO_FIX_AVAILABLE` | All known fix strategies exhausted | `recommendation = RESTRUCTURE`. `fix_type = NO_FIX`. Full diagnostic context to Scenario Builder. |
| `CONFLICTING_FIXES` | Two IIS constraints require contradictory relaxations | Apply fix for the binding constraint with broader impact. Log conflict. |
| `CIRCULAR_FIX_LOOP` | Same fix applied 3+ times with same outcome | `recommendation = RESTRUCTURE`. Flag `known_failure_pattern` in Iteration Memory. |
| `ITERATION_MEMORY_UNAVAILABLE` | Cannot read prior failure patterns | Proceed but disable prior-attempt escalation. Flag `WARN: NO_HISTORY`. |
| `STRUCTURAL_INFEASIBILITY` | Physical analysis confirms fix is impossible | `recommendation = RESTRUCTURE`. Explain physical limitation in `explanation`. |

---

## 9. Example

**Input (timeout, second occurrence):**
```yaml
refiner_input:
  run_id: "run_id_005"
  trigger_context: "TIMEOUT"
  log_analysis_report:
    overall_status: "TIMEOUT_SUBOPTIMAL"
    convergence:
      final_mip_gap: 0.042
      convergence_rate: "SLOW"
      bound_quality: "LOOSE"
  scenario_config:
    solver_settings:
      mip_gap: 0.001
      time_limit_s: 600
      warm_start: false
  iteration_history:
    known_failure_patterns: []
```

**Output:**
```yaml
refiner_output:
  run_id: "run_id_005"
  recommendation: "CONTINUE"
  fix_type: "SOLVER_TUNING"
  confidence: "HIGH"
  refinement_overrides:
    solver_settings:
      mip_gap: 0.001
      time_limit_s: 900
      warm_start: true
      CutPasses: 5
      Cuts: 2
  explanation: "Solver timed out at MIP gap 4.2% after 600 s. LP bound is loose (>10% gap). Increasing time limit to 900 s, enabling warm start from best incumbent, and applying aggressive cutting planes to tighten the LP bound."
  fix_history_entry:
    iteration: 3
    trigger: "TIMEOUT"
    cause: "SLOW_CONVERGENCE_LOOSE_BOUND"
    fix_applied: "time_limit: 600 → 900, warm_start: true, CutPasses: 5, Cuts: 2"
    prior_attempts: 0
```
