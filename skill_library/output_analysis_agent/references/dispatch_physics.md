# Dispatch Physics Reference

Foundational physical and economic definitions used by the Output Analysis Agent when validating solver solutions.

---

## 1. Power Balance

**Definition:** At every time step `t`, the total generation plus load shedding must exactly equal demand:

```
sum_g dispatch_MW[g,t] + load_shedding_MW[t] = demand_MW[t]   for all t
```

**Why it must hold exactly:** Power balance is an equality constraint in the optimization model with a very high penalty for load shedding. If the solver returns a solution that violates power balance by more than numerical tolerance, the solver has a correctness bug or the solution was corrupted during parsing.

**Tolerance in the output analyzer:**
- Production/real solver: 0.01 MW (Gurobi numerical tolerance)
- Mock/MVP: 0.50 MW (rounding from floating-point dispatch arithmetic)

**What to do if violated:** Route immediately to model review. Do NOT accept the solution. This is not a fixable issue via parameter tuning — it indicates a model construction or parsing error.

---

## 2. Generator Dispatch Bounds

For thermal generators (committed = u[g,t] = 1):
```
P_min[g] × u[g,t] ≤ dispatch_MW[g,t] ≤ P_max[g] × u[g,t]
```

For variable renewable energy (VRE) generators:
```
0 ≤ dispatch_MW[g,t] ≤ P_max_timeseries[g,t]   (capacity factor × nameplate)
```

**P_min physical interpretation:** The minimum stable generation level of a thermal unit. Below P_min, combustion becomes unstable and the unit must shut down. Common values:
- Gas CC: 30% of P_max (e.g., 180 MW for a 600 MW unit)
- Gas CT: 20% of P_max (e.g., 40 MW for a 200 MW unit)
- Coal: 40–50% of P_max
- Nuclear: 90% of P_max (effectively baseload)

---

## 3. Ramp Rates

**Definition:** The maximum rate of change in output between consecutive time steps:
```
dispatch_MW[g,t] - dispatch_MW[g,t-1] ≤ ramp_up_MW_h × dt_h    (ramp up limit)
dispatch_MW[g,t-1] - dispatch_MW[g,t] ≤ ramp_down_MW_h × dt_h  (ramp down limit)
```

For hourly resolution (dt_h = 1.0), `ramp_MW_h` equals the maximum MW change per hour.

**Physical interpretation:** Ramp rates are limited by turbine thermal stress, steam drum pressure limits (steam units), and combustion stability. Violating ramp limits in a solution indicates the ramp constraint was not applied correctly in the model.

**Typical values:**

| Generator type | Ramp up (MW/min) | Ramp up (MW/h) for 600 MW unit |
|---|---|---|
| Gas CC | 6–12 MW/min | 360–720 MW/h |
| Gas CT | 3–5 MW/min | 180–300 MW/h |
| Coal | 2–4 MW/min | 120–240 MW/h |
| Nuclear | 1–3 MW/min | 60–180 MW/h |
| Battery | 100% in < 1 min | Full capacity in one step |

---

## 4. Spinning Reserve

**Definition:** The reserve that can be provided by online (committed) units within 10 minutes (or one time step, whichever is shorter):
```
sum_g (P_max[g,t] - dispatch_MW[g,t]) × u[g,t] ≥ spin_MW[t]
```

Where `spin_MW[t] = demand_MW[t] × spinning_reserve_pct`.

**Physical interpretation:** Spinning reserve is the online headroom available to respond instantly (within seconds to minutes) to a sudden generation loss. It must come from already-committed, online generators — offline units cannot contribute (they take minutes to hours to start).

**Typical requirement:** 5% of load in ERCOT; 5% + 1,800 MW in PJM.

---

## 5. Marginal Prices (Shadow Prices)

**Definition:** The Lagrange multiplier (dual variable) on the power balance constraint. Represents the cost of serving one additional MWh of demand.

**For MIP models with binary commitment:** Marginal prices from MIP solutions are NOT true shadow prices. They are "approximate" because the LP relaxation at the optimal integer solution may not reflect the true dual problem. Tag as `approximate` in all reports.

**Interpretation guidelines:**
- Price = `cost_linear_USD_per_MWh` of the marginal generator: normal
- Price = 0: excess renewable generation (VRE curtailed, no thermal committed)
- Price >> `max cost_linear`: either a binding reserve constraint or a binding emission cap is driving the price above variable cost
- Negative price: over-generation (renewables producing more than demand — curtailment should have occurred)

---

## 6. Emission Accounting

**Total emissions:**
```
total_emissions_tCO2 = sum_t sum_g emission_rate[g] × dispatch_MW[g,t] × dt_h
```

**Verification check:**
```
|reported_total - computed_from_dispatch| / reported_total < 0.001   (0.1%)
```

If this discrepancy exceeds 0.1%, the emission accounting in the model is inconsistent with the dispatch solution — flag `EMISSION_ACCOUNTING_ERROR`.

**Renewable fraction:**
```
renewable_fraction = sum_t sum_{g in VRE} dispatch_MW[g,t] / sum_t demand_MW[t]
```

This is an energy-based metric (MWh / MWh), not a capacity-based metric.

---

## 7. Curtailment vs. Load Shedding

| Concept | Definition | Sign | Cost |
|---|---|---|---|
| **Curtailment** | VRE output withheld below available capacity | `VRE_available - VRE_dispatched ≥ 0` | Zero or small operational cost |
| **Load shedding** | Demand not served | `demand - supply ≥ 0` | Very high penalty ($10,000/MWh typical) |

**Key rule:** Curtailment should always be preferred over load shedding when there is an over-supply of VRE. If the output analyzer finds `curtailment > 0` and `load_shedding > 0` simultaneously, this is a modeling error — the solver is shedding load while curtailing free renewable energy.

---

## 8. Cost Plausibility Benchmarks

For a fleet with thermal units at cost `$c/MWh` and `f` fraction renewable:

| Metric | Expected range |
|---|---|
| `cost_per_MWh_avg` | `(1-f) × min_thermal_cost` to `max_thermal_cost × 1.5` |
| `total_cost_USD` | `(1-f) × total_demand × min_mc` to `total_demand × max_mc × 1.5` |

Where `f` is the renewable fraction in the optimal dispatch. With 62% renewables and a $20.80/MWh gas CC, expect `0.38 × 20.80 = $7.90/MWh` average — not $20.80/MWh, since 62% of energy is zero-cost.
