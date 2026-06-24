---
name: output_analysis_agent
description: "Use this skill to validate solver dispatch outputs for physical feasibility, economic plausibility, emissions behavior, and anomalies."
---

# Skill: Output Analysis Agent

## 1. Skill Name
**Output Analysis Agent**

---

## 2. Purpose
Validates the economic and physical plausibility of the solver's dispatch solution. Detects unrealistic dispatch patterns, cost and emission anomalies, and constraint violations that may indicate modeling errors, numerical artifacts, or solver issues. Produces a structured quality report consumed by the Supervisory Controller and Refiner Agent.

---

## 3. When to Use
Invoke this skill:
- After every successful (OPTIMAL or TIME_LIMIT) Model Executor run, after Solver Log Analyzer completes
- Only when `executor_output.solver_termination_status` is `OPTIMAL` or `TIME_LIMIT` and a solution exists
- Do **not** invoke if solution is null (infeasible runs route directly to Refiner via Solver Log Analyzer)

---

## 4. Inputs

```yaml
output_analysis_input:
  run_id: "run_id_001"
  dispatch_csv_path: "runs/run_id_001/dispatch.csv"
  summary_json_path: "runs/run_id_001/output_summary.json"
  scenario_config:
    generators:
      - id: "gas_cc_1"
        capacity_MW: 600
        P_min_MW: 180
        ramp_up_MW_h: 480
        ramp_down_MW_h: 480
        cost_linear_USD_per_MWh: 20.80
        emission_rate_tCO2_per_MWh: 0.35
        type: "gas_combined_cycle"
      - id: "solar_1"
        capacity_MW: 650
        cost_linear_USD_per_MWh: 0.0
        type: "solar"
      - id: "wind_1"
        capacity_MW: 900
        cost_linear_USD_per_MWh: 0.0
        type: "wind"
    demand_MW: [525, 499, 480, 525, 656, 764, 817, 898, 956, 984, ...]
    policy_constraints:
      emission_cap_tCO2: 5000
      renewable_penetration_min: 0.60
  prior_analysis_report: null
```

---

## 5. Outputs

```yaml
output_analysis_report:
  run_id: "run_id_001"
  analyzed_at: "2024-01-01T08:35:00Z"
  overall_status: "OK"                 # OK | WARN | ANOMALY
  power_balance:
    violations: []
    max_imbalance_MW: 0.0
    status: "OK"
  dispatch_bounds:
    below_pmin_events: []
    above_pmax_events: []
    zero_generation_generators: []
    status: "OK"
  ramp_rates:
    ramp_violations: []
    status: "OK"
  cost_analysis:
    objective_value_USD: 163249.0
    cost_per_MWh_avg: 8.71
    benchmark_low_USD_per_MWh: 10.40
    benchmark_high_USD_per_MWh: 94.08
    computed_variable_cost_USD: 163249.0
    anomalies: []
    status: "OK"
  emission_analysis:
    total_tCO2: 2752.0
    cap_tCO2: 5000
    slack_pct: 44.96
    anomalies: []
    status: "OK"
  renewable_analysis:
    fraction_achieved: 0.622
    target: 0.60
    target_met: true
    anomalies: []
    status: "OK"
  load_shedding:
    total_MWh: 0.0
    shed_pct_of_demand: 0.0
    periods: []
    anomalies: []
    status: "NONE"
  all_anomalies: []
  recommendations:
    - "All checks passed. Solution is physically plausible."
```

---

## 6. Step-by-Step Instructions

1. **Power balance check.**
   - For each timestep `t`: `supply[t] = sum_g dispatch_MW[g,t] + load_shedding[t]`.
   - `imbalance[t] = |supply[t] - demand_MW[t]|`.
   - If `max(imbalance) > 0.5 MW`: flag `ANOMALY: POWER_BALANCE_VIOLATION`. This is a critical error — solver is incorrect.

2. **Generator dispatch bound checks.**
   - For each thermal generator: if `dispatch_MW[g,t] > 0 AND dispatch_MW[g,t] < P_min[g] - 0.5`: flag `BELOW_PMIN`.
   - If `dispatch_MW[g,t] > P_max[g] + 0.5`: flag `ABOVE_PMAX`.
   - For VRE: check `dispatch_MW[g,t] <= profile_CF[t] * capacity_MW[g] + 0.5`.
   - Any violation → `overall_status = ANOMALY`.

3. **Ramp rate check.**
   - For each thermal generator, consecutive period `(t, t+1)`:
     - `delta = dispatch_MW[g,t+1] - dispatch_MW[g,t]`
     - `delta > ramp_up_MW_h + 0.5` → `RAMP_UP_VIOLATION`
     - `delta < -ramp_down_MW_h - 0.5` → `RAMP_DOWN_VIOLATION`

4. **Cost plausibility check.**
   - `cost_per_MWh_avg = objective_value_USD / total_demand_MWh`
   - Compute `thermal_fraction` (thermal energy / total energy).
   - `effective_low = min_mc * 0.5 * thermal_fraction`; `effective_high = max_mc * 3.0`
   - If `cost_per_MWh_avg < effective_low`: `LOW_COST_ANOMALY`.
   - If `cost_per_MWh_avg > effective_high`: `HIGH_COST_ANOMALY`.
   - Cross-check: recompute cost from dispatch. If discrepancy > 5%: flag `COST_DISCREPANCY`.

5. **Emission analysis.**
   - Verify `total_tCO2 <= emission_cap_tCO2`. Violation → `OVER_CAP` (`ANOMALY`).
   - `slack_pct = (cap - total) / cap * 100`. If `slack_pct < 5`: flag `TIGHT_EMISSION_CAP` (`WARN`).

6. **Renewable penetration check.**
   - `fraction = sum_VRE_dispatch / sum_demand`. If `fraction < target - 0.001`: `RENEWABLE_TARGET_MISSED` (`ANOMALY`).

7. **Load shedding check.**
   - Any shedding > 0 → minimum `WARN`. If > 1% of total demand → `ANOMALY`.

8. **Aggregate and emit.**
   - `ANOMALY` beats `WARN` beats `OK`. Populate `all_anomalies` and `recommendations`.

---

## 7. Heuristics / Rules

- **Power balance tolerance**: 0.5 MW (mock rounding tolerance); tighten to 0.01 MW for production runs.
- **Ramp tolerance**: 0.5 MW floating-point tolerance on all ramp checks.
- **Never-dispatched thermal**: A thermal unit that outputs 0 MW across all 24 periods is unusual — flag `WARN` (possible over-constraint or infeasible minimum generation floor).
- **Renewables + load shedding**: If any VRE curtailment > 0 AND load shedding > 0 simultaneously: flag `ANOMALY` — contradictory; curtail before shedding load.
- **Load shedding warn threshold**: 1 MWh total; anomaly threshold: 1% of total demand.
- **Cost benchmark**: Low = `min_thermal_mc * 0.5 * thermal_fraction`; High = `max_thermal_mc * 3.0`.

---

## 8. Failure Modes

| Failure | Condition | Response |
|---|---|---|
| `NULL_SOLUTION` | `executor_output.solution` is null | Do not invoke. Route directly to Refiner Agent. |
| `MISSING_DISPATCH_SERIES` | Generator in config but absent from dispatch CSV | Flag `ANOMALY: MISSING_GENERATOR_OUTPUT`. |
| `DIMENSION_MISMATCH` | Dispatch timeseries length ≠ T | Flag `ANOMALY: TIMESERIES_LENGTH_ERROR`. |
| `POWER_BALANCE_VIOLATION` | Supply ≠ demand at any step (> 0.5 MW) | Flag `ANOMALY`. Escalate to model review — solver correctness issue. |
| `ANALYSIS_COMPUTE_ERROR` | Exception during numerical checks | Return partial report with `overall_status = WARN`. |

---

## 9. Example

**Input (dispatch with ramp violation):**
```yaml
output_analysis_input:
  run_id: "run_id_004"
  scenario_config:
    generators:
      - id: "gas_cc_1"
        capacity_MW: 600
        P_min_MW: 180
        ramp_up_MW_h: 120
        ramp_down_MW_h: 120
        type: "gas_combined_cycle"
```
*(dispatch shows gas_cc_1 jumping from 180 MW to 400 MW in one step — 220 MW delta, limit 120 MW/h)*

**Output:**
```yaml
output_analysis_report:
  run_id: "run_id_004"
  overall_status: "ANOMALY"
  ramp_rates:
    ramp_violations:
      - generator: "gas_cc_1"
        period: "t=4→t=5"
        delta_MW: 220.0
        limit_MW_h: 120.0
        type: "RAMP_UP_VIOLATION"
    status: "ANOMALY"
  all_anomalies:
    - "RAMP: gas_cc_1 ramps 220 MW in 1 hour, exceeding limit of 120 MW/h at t=5."
  recommendations:
    - "Critical anomalies detected — do not use this solution. Route to Refiner Agent."
```
