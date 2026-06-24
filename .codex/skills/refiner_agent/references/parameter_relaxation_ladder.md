# Parameter Relaxation Ladder

An ordered list of relaxations the Refiner Agent should attempt, from least to most disruptive. Work down the ladder: only escalate when the prior level fails to produce a feasible solution.

The ladder applies separately for each failure mode. Do not mix rungs across failure modes.

---

## Ladder A: EMISSION_CAP_TOO_TIGHT

| Rung | Action | Expected impact | Stop condition |
|---|---|---|---|
| A1 | Relax `emission_cap_tCO2` by 20% | Removes most infeasibility cases where cap is slightly too tight | Try next iteration |
| A2 | Relax `emission_cap_tCO2` by 40% total + reduce `renewable_penetration_min` by 0.05 | Joint relaxation breaks combined IIS | Try next iteration |
| A3 | Set `emission_cap_tCO2 = min_achievable_emissions × 1.10` + `renewable_penetration_min` to max achievable | Minimum feasible cap | Try next iteration |
| A4 | RESTRUCTURE: add battery storage or additional VRE capacity | Reduces thermal obligation, lowers minimum achievable emissions | Requires Scenario Builder rebuild |

**Never go below:** `emission_cap_tCO2 < sum_t max(0, demand[t] - sum_VRE_max[t]) × min_thermal_emission_rate`

---

## Ladder B: DEMAND_SUPPLY_MISMATCH

| Rung | Action | Expected impact | Stop condition |
|---|---|---|---|
| B1 | Reduce `spinning_reserve_pct` from 5% to 3% | Frees 2% capacity for dispatch | Try next iteration |
| B2 | Check if a generator was accidentally disabled; re-enable it | Restore intended fleet capacity | Immediate fix |
| B3 | RESTRUCTURE: add 200 MW gas CT peaking unit | Adds fast-response capacity | Requires Scenario Builder rebuild |
| B4 | RESTRUCTURE: add 100 MW battery (4h) | Adds flexible capacity with no emissions | Requires Scenario Builder rebuild |

**Note:** If `sum_g P_max[g] < max(demand_MW) × 1.05`, skip B1–B2 and go directly to B3.

---

## Ladder C: TIMEOUT / SLOW CONVERGENCE

| Rung | Action | Expected impact | Stop condition |
|---|---|---|---|
| C1 | `time_limit_s × 1.5` + warm start if prior solution exists | Allow more time for convergence | Accept result at next iteration |
| C2 | `mip_gap × 2` + `Cuts: 1` + `CutPasses: 3` | Accept slightly lower quality; improve LP bound | Accept result at tighter gap |
| C3 | `Cuts: 2` + `CutPasses: 5` + `Heuristics: 0.1` | Aggressive search | Accept result |
| C4 | `binary_commitment: false` (LP relaxation) | No binary variables — solve much faster, approximate | Flag `WARN: LP_RELAXATION_USED`, accept |
| C5 | Reduce problem resolution: `T: 12` (12h instead of 24h) + RESTRUCTURE | Smaller problem, faster solve | Requires Scenario Builder rebuild |

**Never:** Increase `mip_gap` above 5% (0.05) for production planning. LP relaxation (C4) is a last resort.

---

## Ladder D: NUMERICAL_INSTABILITY

| Rung | Action | Expected impact | Stop condition |
|---|---|---|---|
| D1 | `NumericFocus: 2`, `ScaleFlag: 2` | More careful pivoting, coefficient scaling | Try next iteration |
| D2 | `NumericFocus: 3`, `ScaleFlag: 2`, `ObjScale: -0.5` | Maximum numerical care | Try next iteration |
| D3 | Normalize units: convert capacity from MW to GW (divide all capacity/demand by 1000) + RESTRUCTURE | Reduces coefficient range by 1000× | Requires Scenario Builder rebuild |
| D4 | Switch to HiGHS open-source solver (if Gurobi license issue) | Different solver, may handle the problem better | Try next iteration |

**When to apply D3:** If coefficient range ratio > 1e6 (e.g., startup cost = $12,000 vs. hourly demand = 1,100 MW). Converting to GW reduces the ratio by 1,000.

---

## Ladder E: OUTPUT_ANOMALY (RAMP_VIOLATION)

| Rung | Action | Expected impact | Stop condition |
|---|---|---|---|
| E1 | Add `ramp_constraint_debug: true` to solver hints; re-run with verbose constraint logging | Identify which constraint was misapplied | Diagnostic only |
| E2 | Verify `ramp_up_MW_h` and `ramp_down_MW_h` were correctly set in `scenario_config`; re-emit corrected config | Fix data entry error | Try next iteration |
| E3 | If ramp values are correct: check constraint index mapping in JuMP model | Model construction error | Requires model code fix |
| E4 | RESTRUCTURE: increase ramp rates by 20% to match actual physical limits | Relax over-tight ramp assumption | Requires Scenario Builder rebuild |

---

## General Safeguards

1. **Targeted fix first**: Change only the minimum set of parameters to address the specific IIS. Do not shotgun multiple relaxations simultaneously.

2. **Maximum relaxation per parameter per attempt**: Never relax any single parameter by more than 50% in one step. Aggressive relaxation obscures the root cause.

3. **Never over-tighten**: Do not tighten constraints during a refinement cycle unless the output analysis shows > 20% slack in that constraint. Tightening reduces solution quality.

4. **Escalation limit**: After 3 attempts at the same failure cause, stop parameter relaxation and trigger RESTRUCTURE.

5. **Emission floor protection**: Never propose `emission_cap_tCO2` below the minimum achievable given the current fleet. Compute this floor before recommending any emission cap relaxation.

6. **Warm start eligibility**: Only enable warm start if:
   - Prior solution was feasible (OPTIMAL or TIME_LIMIT with incumbent)
   - No structural changes were made (new generators, different topology)
   - Refiner only changed parameter values, not model formulation

---

## Confidence Scoring by Rung

| Rung | Confidence |
|---|---|
| A1, B1, C1, D1 | HIGH — well-established fix for known cause |
| A2, B2, C2, D2 | HIGH — escalated version of proven fix |
| A3, B3, C3, D3 | MEDIUM — multiple causes possible |
| A4, B4, C4, D4 | MEDIUM/LOW — structural fix, uncertain impact |
| RESTRUCTURE | LOW — requires human validation of new scenario |
