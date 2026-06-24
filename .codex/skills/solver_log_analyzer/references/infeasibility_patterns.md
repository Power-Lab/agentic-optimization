# Infeasibility Patterns Reference

A catalog of common infeasibility patterns encountered in energy system unit commitment / economic dispatch models, their root causes, and recommended fixes. Used by the Solver Log Analyzer to classify `likely_cause` and by the Refiner Agent to select fix strategies.

---

## 1. Pattern: EMISSION_CAP_TOO_TIGHT

**IIS signature:** `emission_cap[total]` + `renewable_penetration_minimum` (often co-occurring)

**Root cause:**
The emission cap requires a very low carbon output, but meeting the renewable penetration minimum requires committing thermal units during periods when renewable generation is insufficient (e.g., pre-dawn hours, low-wind periods). The thermal generation needed to maintain system reliability produces emissions that exceed the cap.

**Minimum feasible emissions calculation:**
```
min_emissions ≈ sum_t max(0, demand[t] - sum_VRE_max[t]) × min_thermal_emission_rate_tCO2_per_MWh
```
If `emission_cap < min_emissions`, the problem is physically infeasible.

**Recommended fixes (in priority order):**
1. Relax `emission_cap_tCO2` by 20% per attempt
2. Reduce `renewable_penetration_min` by 0.05 per attempt
3. If both relaxations fail twice: add storage (battery dispatch as peaking resource) — RESTRUCTURE
4. If physical floor is hit: RESTRUCTURE with higher VRE capacity

---

## 2. Pattern: DEMAND_SUPPLY_MISMATCH

**IIS signature:** `power_balance[t=*]` for ≥ 30% of timesteps

**Root cause:**
Total installed capacity is insufficient to meet demand plus spinning reserve requirements. This can occur when:
- The demand profile was scaled up without adding corresponding capacity
- A large thermal unit was disabled (outage modeled incorrectly)
- The spinning reserve requirement is too aggressive for the fleet

**Detection:**
```
Check: sum_g P_max[g] >= max(demand_MW) * 1.05 + max(spin_MW)
```
If not satisfied, the model is structurally infeasible — no parameter fix will help.

**Recommended fixes:**
1. Add peaking capacity (gas CT or diesel) — RESTRUCTURE
2. Reduce `spinning_reserve_pct` from 5% to 3% as a first attempt
3. If demand was incorrectly scaled: correct `peak_MW` in scenario config

---

## 3. Pattern: COMMITMENT_RAMP_INFEASIBILITY

**IIS signature:** `ramp[g,t]` + `commitment[g,t]` + `min_up_time[g,t]`

**Root cause:**
A generator that is committed (must remain on due to min-up-time) cannot ramp down fast enough to respect the demand decrease AND cannot ramp up fast enough to meet sudden demand increases. Common in scenarios with:
- Fast demand ramps (duck curve evening climb)
- Slow thermal generators with large min-up-time
- Insufficient fast-response capacity

**Recommended fixes:**
1. Increase `ramp_up_MW_h` and `ramp_down_MW_h` by 10% per attempt
2. Reduce `min_up_time_h` and `min_down_time_h` by 1 hour per attempt
3. If the demand ramp genuinely exceeds system ramp capacity: add fast-ramping CT or battery — RESTRUCTURE

---

## 4. Pattern: RESERVE_INFEASIBILITY

**IIS signature:** `spinning_reserve[t=*]` for specific hours (typically evening peak)

**Root cause:**
During peak demand hours, all generators are dispatched near P_max to meet demand, leaving insufficient headroom for spinning reserve. This is particularly common when:
- `spinning_reserve_pct` is high (> 10%)
- The fleet has limited peaking units
- VRE is not available during peak hours (solar absent at t=18–20)

**Recommended fixes:**
1. Reduce `spinning_reserve_pct` from 5% to 3%
2. Add an online peaking unit with headroom — RESTRUCTURE
3. Model VRE as partially counted toward spinning reserve (common in some jurisdictions)

---

## 5. Pattern: RENEWABLE_TARGET_UNREACHABLE

**IIS signature:** `renewable_penetration_minimum` alone (no power balance issues)

**Root cause:**
The integrated VRE energy potential (sum of CF × capacity over all hours) is less than `renewable_penetration_min × total_demand`. This is a physical limit, not a modeling error.

**Check:**
```
max_achievable_ren_fraction = sum_t(sum_VRE max_output[t]) / sum_t(demand[t])
if max_achievable_ren_fraction < renewable_penetration_min: STRUCTURALLY_INFEASIBLE
```

**Recommended fixes:**
1. Reduce `renewable_penetration_min` to `max_achievable_ren_fraction - 0.02`
2. Add more VRE capacity — RESTRUCTURE

---

## 6. IIS Constraint ID Mapping

| Constraint ID Pattern | Model constraint | Likely issue |
|---|---|---|
| `emission_cap[total]` | Global emission cap | Cap too tight |
| `power_balance[t=N]` | Supply = demand at hour N | Capacity shortage |
| `renewable_penetration_minimum` | Sum VRE ≥ target × demand | VRE insufficient |
| `ramp_up[g,t=N]` | p[g,t] - p[g,t-1] ≤ ramp_up | Generator ramp too slow |
| `ramp_down[g,t=N]` | p[g,t-1] - p[g,t] ≤ ramp_down | Generator ramp too slow |
| `min_up_time[g,t=N]` | u[g,t] ≥ u[g,t-1] - w[g,t] | Unit must stay on |
| `min_down_time[g,t=N]` | u[g,t] ≤ 1 - v[g,t] | Unit must stay off |
| `spinning_reserve[t=N]` | Headroom ≥ spin_MW | Insufficient online capacity |
| `pmax[g,t=N]` | p[g,t] ≤ P_max × u[g,t] | Commitment-dispatch conflict |
| `pmin[g,t=N]` | p[g,t] ≥ P_min × u[g,t] | Minimum generation floor |

---

## 7. Escalation Logic

| Failure count for same cause | Action |
|---|---|
| 1st failure | Apply targeted fix (20% relaxation) |
| 2nd failure | Escalate magnitude (40% relaxation, also relax secondary constraint) |
| 3rd failure | RESTRUCTURE — parameter relaxation is exhausted |

Never relax `emission_cap_tCO2` below the physical minimum achievable emissions floor. If the floor is hit on the first attempt, skip directly to RESTRUCTURE.
