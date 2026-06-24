---
name: scenario_builder
description: "Use this skill to turn high-level energy system optimization goals into validated solver-ready scenario configuration files."
---

# Skill: Scenario Builder Agent

## 1. Skill Name
**Scenario Builder Agent**

---

## 2. Purpose
Constructs a fully specified, solver-ready energy system scenario from high-level user inputs or optimization goals. Translates abstract problem descriptions (e.g., "minimize cost for a 24-hour ERCOT horizon with 60% renewable penetration and a 5,000 tCO2 cap") into structured YAML configuration files that the Model Executor can consume directly.

Also handles **re-runs**: when the Refiner Agent proposes changes, this skill merges refinement overrides into the prior base config and re-validates the result before returning a new `scenario_config`.

---

## 3. When to Use
Invoke this skill:
- At the start of every optimization session (iteration 0)
- When the Refiner Agent issues `recommendation = RESTRUCTURE`
- When any high-level planning parameter changes (demand profile, fuel prices, emission caps, reserve margins)
- When Iteration Memory flags a structural reset is required

Do **not** invoke for parameter-only changes (emission cap relaxation, solver hint tuning) — those are applied directly via `refinement_overrides` without rebuilding the full scenario.

---

## 4. Inputs

```yaml
scenario_request:
  horizon:
    start: "2024-01-01T00:00:00Z"      # ISO-8601
    end:   "2024-01-02T00:00:00Z"
    resolution_minutes: 60
  region: "ERCOT"                      # ERCOT | CAISO | PJM | MISO | SPP | NYISO
  objectives:
    primary: "minimize_cost"           # minimize_cost | minimize_emissions | multi_objective
    emission_cap_tCO2: 5000            # null = unconstrained
    renewable_penetration_min: 0.60   # fraction [0,1]; null = unconstrained
  generators:
    - id: "gas_cc_1"
      type: "gas_combined_cycle"
      capacity_MW: 600
      min_up_time_h: 4
      min_down_time_h: 4
      ramp_rate_MW_per_min: 8
      heat_rate_MMBtu_per_MWh: 6.5
      fuel_cost_USD_per_MMBtu: 3.20
      startup_cost_USD: 12000
      emission_rate_tCO2_per_MWh: 0.35
    - id: "gas_ct_1"
      type: "gas_combustion_turbine"
      capacity_MW: 200
      min_up_time_h: 1
      min_down_time_h: 1
      ramp_rate_MW_per_min: 3.33
      heat_rate_MMBtu_per_MWh: 9.8
      fuel_cost_USD_per_MMBtu: 3.20
      startup_cost_USD: 3000
      emission_rate_tCO2_per_MWh: 0.55
    - id: "solar_1"
      type: "solar"
      capacity_MW: 650
      profile_source: "synthetic_summer"
    - id: "wind_1"
      type: "wind"
      capacity_MW: 900
      profile_source: "synthetic_moderate"
  demand:
    profile_source: "synthetic_typical_day"
    peak_MW: 1100
  reserves:
    spinning_reserve_pct: 0.05
    non_spinning_reserve_pct: 0.03
  network:
    topology: "single_bus"
  refinement_overrides: {}             # Populated by Refiner Agent on re-runs
  iteration_history_ref: null          # run_id of prior iteration; null on first run
```

---

## 5. Outputs

```yaml
scenario_config:
  run_id: "run_id_001"
  created_at: "2024-01-01T08:00:00Z"
  scenario_name: "scenario_run_id_001"
  source_request: "Minimize cost for ERCOT 24h horizon with 60% renewables and 5000 tCO2 cap"
  region: "ERCOT"
  model_type: "unit_commitment"
  time_horizon:
    start: "2024-01-01T00:00:00Z"
    end:   "2024-01-02T00:00:00Z"
    resolution_minutes: 60
    T: 24
    dt_h: 1.0
  demand_assumptions:
    peak_MW: 1100
    profile: "synthetic_typical_day"
  generators:
    - id: "gas_cc_1"
      type: "gas_combined_cycle"
      capacity_MW: 600
      P_min_MW: 180
      heat_rate_MMBtu_per_MWh: 6.5
      fuel_cost_USD_per_MMBtu: 3.20
      cost_linear_USD_per_MWh: 20.80
      cost_fixed_USD_per_h: 850
      startup_cost_USD: 12000
      min_up_time_h: 4
      min_down_time_h: 4
      ramp_up_MW_h: 480
      ramp_down_MW_h: 480
      emission_rate_tCO2_per_MWh: 0.35
    - id: "gas_ct_1"
      type: "gas_combustion_turbine"
      capacity_MW: 200
      P_min_MW: 40
      heat_rate_MMBtu_per_MWh: 9.8
      fuel_cost_USD_per_MMBtu: 3.20
      cost_linear_USD_per_MWh: 31.36
      cost_fixed_USD_per_h: 300
      startup_cost_USD: 3000
      min_up_time_h: 1
      min_down_time_h: 1
      ramp_up_MW_h: 200
      ramp_down_MW_h: 200
      emission_rate_tCO2_per_MWh: 0.55
    - id: "solar_1"
      type: "solar"
      capacity_MW: 650
      cost_linear_USD_per_MWh: 0.0
      emission_rate_tCO2_per_MWh: 0.0
    - id: "wind_1"
      type: "wind"
      capacity_MW: 900
      cost_linear_USD_per_MWh: 0.0
      emission_rate_tCO2_per_MWh: 0.0
  policy_constraints:
    emission_cap_tCO2: 5000
    renewable_penetration_min: 0.60
    spinning_reserve_pct: 0.05
    non_spinning_reserve_pct: 0.03
    network_model: "single_bus"
  solver_settings:
    mip_gap: 0.001
    time_limit_s: 600
    warm_start: false
    binary_commitment: true
    load_shedding_penalty_USD_per_MWh: 10000
  refinement_overrides: {}
  validation_status: "PASS"            # PASS | WARN | FAIL
  validation_warnings: []
```

---

## 6. Step-by-Step Instructions

1. **Parse and validate the scenario request.**
   - Confirm `horizon.start < horizon.end` and `resolution_minutes` divides the horizon evenly.
   - Check all generator IDs are unique. Raise `FAIL: DUPLICATE_GENERATOR_IDS` if not.
   - If `profile_source` is `"file:<path>"`, confirm the file exists and has `T` rows.

2. **Resolve demand and VRE timeseries.**
   - For `synthetic_typical_day`: generate a duck-curve profile with peak at `t=18`, shoulder at `t=9`, valley at `t=4`. Scale to `peak_MW`.
   - For `synthetic_summer` solar: sinusoidal from t=6 to t=19, peak CF ≈ 0.95 at solar noon.
   - For `synthetic_moderate` wind: uniform random CF in [0.30, 0.46] seeded deterministically.
   - If a real file is specified, resample to `resolution_minutes` (mean for CFs, sum for demand).

3. **Derive implicit generator parameters.**
   - `cost_linear_USD_per_MWh = heat_rate * fuel_cost`
   - `P_min_MW` from type heuristics (if not provided):
     - Gas CC: 30% of P_max
     - Gas CT: 20% of P_max
     - Coal: 40% of P_max
     - Nuclear: 90% of P_max
   - `ramp_up_MW_h = ramp_rate_MW_per_min * 60`

4. **Apply refinement overrides.**
   - Deep-merge `refinement_overrides` over the base config. Log every changed key.
   - Re-derive `cost_linear_USD_per_MWh` if `fuel_cost` was overridden.

5. **Check feasibility preconditions.**
   - `sum(P_max) >= max(demand_MW) * 1.05 + max(spin_MW)`. Raise `FAIL: INSUFFICIENT_CAPACITY` if not.
   - If `renewable_penetration_min` is set, check that integrated VRE energy ≥ target × total demand. Raise `WARN: RENEWABLE_PENETRATION_MARGINAL` if within 5%.
   - If `emission_cap_tCO2 < 1.1 × min_achievable_emissions`, raise `WARN: TIGHT_EMISSION_CAP`.

6. **Assign solver hints.**
   - Defaults: `mip_gap=0.001`, `time_limit_s=600`.
   - If prior iteration timed out (from `iteration_history_ref`): `time_limit_s *= 1.5`.
   - If prior `mip_gap_final > 0.01`: `mip_gap = mip_gap_final * 0.5`.

7. **Emit `scenario_config`.**
   - Assign new `run_id`. Set `validation_status` from worst finding.
   - Set `created_at` to current UTC timestamp.

---

## 7. Heuristics / Rules

- **P_min floor**: Never allow `P_min_MW = 0` for thermal units. Minimum 10% of P_max even if not explicitly specified.
- **Renewable overgeneration risk**: If `sum_g(P_max_VRE[t]) > demand_MW[t]` for > 20% of periods, flag `WARN: CURTAILMENT_RISK`.
- **Cold-start horizon check**: If `min_up_time_h + min_down_time_h > T / 4` for any unit, flag `WARN: HEAVY_COMMITMENT_CONSTRAINTS`.
- **Ramp feasibility**: Verify `max(|demand_MW[t+1] - demand_MW[t]|) <= sum_g(ramp_up_MW_h[g])`. Issue `WARN: DEMAND_RAMP_EXCEEDS_FLEET` if not.
- **Reserve sizing**: `spin_MW[t] = demand_MW[t] * spinning_reserve_pct`. This must be achievable by the online fleet headroom.
- **Emission cap floor**: Minimum feasible emissions ≈ `sum_t max(0, demand_MW[t] - sum_VRE_P_max[t]) * min_thermal_emission_rate`. Never set cap below this floor.

---

## 8. Failure Modes

| Failure | Condition | Response |
|---|---|---|
| `MISSING_PROFILE_FILE` | Referenced timeseries file not found | Raise `FAIL`. Request corrected path. |
| `INSUFFICIENT_CAPACITY` | Sum of P_max < peak demand + spinning reserve | Raise `FAIL`. Suggest adding capacity or reducing demand. |
| `TIMESTAMP_MISMATCH` | Profile timestamps don't align with horizon | `FAIL` if > 2 consecutive missing steps; else `WARN` and interpolate. |
| `DUPLICATE_GENERATOR_IDS` | Two generators share the same ID | Raise `FAIL`. Report conflicting IDs. |
| `INFEASIBLE_PRECONDITION` | Renewable target mathematically unachievable | Raise `FAIL`. Report max achievable penetration given fleet. |
| `OVERRIDE_CONFLICT` | Refinement override contradicts a hard constraint | `WARN`. Apply override but log conflict. |

---

## 9. Example

**Input (plain-text request parsed by heuristics):**
```
Minimize cost for ERCOT 24h horizon with 60% renewables and 5000 tCO2 cap
```

**Output `scenario_config` highlights:**
```yaml
run_id: "run_id_001"
region: "ERCOT"
policy_constraints:
  emission_cap_tCO2: 5000
  renewable_penetration_min: 0.60
generators:
  - id: "gas_cc_1"
    capacity_MW: 600
    P_min_MW: 180
    cost_linear_USD_per_MWh: 20.80
    ramp_up_MW_h: 480
    emission_rate_tCO2_per_MWh: 0.35
  - id: "gas_ct_1"
    capacity_MW: 200
    P_min_MW: 40
    cost_linear_USD_per_MWh: 31.36
    emission_rate_tCO2_per_MWh: 0.55
  - id: "solar_1"
    capacity_MW: 650
    cost_linear_USD_per_MWh: 0.0
  - id: "wind_1"
    capacity_MW: 900
    cost_linear_USD_per_MWh: 0.0
solver_settings:
  mip_gap: 0.001
  time_limit_s: 600
validation_status: "PASS"
```
