# Grid Region Defaults

Reference values for the Scenario Builder Agent when constructing scenarios for specific North American grid regions. Use these as defaults when the user does not specify regional parameters.

---

## ERCOT (Electric Reliability Council of Texas)

| Parameter | Typical Value |
|---|---|
| Peak demand (summer) | 70,000–85,000 MW (system); use scaled subset for study area |
| Annual load factor | ~55% |
| Spinning reserve requirement | 2,300 MW absolute OR 5% of load (use whichever is binding) |
| Non-spinning reserve | Additional 2,300 MW |
| Renewable penetration (2024 actual) | ~31% annual average; summer peaks 15–20% |
| Predominant VRE | Wind (West Texas), Solar (West/South Texas) |
| Typical wind CF | 0.38–0.45 (West Texas) |
| Typical solar CF | 0.22–0.28 annual; 0.30–0.35 summer daytime |
| Dominant thermal fleet | Gas CC (~35 GW), Gas CT (~20 GW), small nuclear (~5 GW) |
| Fuel price (gas, 2024) | $2.50–$4.00/MMBtu |
| Average clearing price | $25–$55/MWh (high volatility; up to $9,000/MWh cap) |

**Default scenario scaling (study-area approximation):**
```yaml
demand_assumptions:
  peak_MW: 1100
  profile: synthetic_typical_day
generators:
  - gas_cc: 600 MW, cost $20.80/MWh, emission 0.35 tCO2/MWh
  - gas_ct: 200 MW, cost $31.36/MWh, emission 0.55 tCO2/MWh
  - solar: 650 MW
  - wind: 900 MW
```

---

## CAISO (California ISO)

| Parameter | Typical Value |
|---|---|
| Peak demand (summer) | 45,000–52,000 MW |
| Annual load factor | ~58% |
| Spinning reserve requirement | 5% of load |
| Renewable penetration (2024 actual) | ~45% annual; 80%+ during spring midday |
| Predominant VRE | Solar (Mojave, Central Valley), Wind (Tehachapi, Altamont) |
| Typical solar CF | 0.25–0.32 annual |
| Duck curve severity | HIGH — large morning/evening ramps (10,000+ MW in 3 hours) |
| Fuel price (gas, 2024) | $3.50–$5.50/MMBtu |
| Average clearing price | $30–$70/MWh |

**Notable CAISO modeling considerations:**
- Curtailment is frequent during spring (over-generation events)
- Evening ramp is the binding operational constraint, not peak demand
- Storage (batteries) is significant and growing — add BESS units for realistic CAISO scenarios
- Net load profile: negative midday possible during spring; model curtailment explicitly

---

## PJM (PJM Interconnection)

| Parameter | Typical Value |
|---|---|
| Peak demand (summer) | 145,000–165,000 MW |
| Annual load factor | ~56% |
| Spinning reserve requirement | 1,800 MW + 5% of load |
| Renewable penetration (2024 actual) | ~10% (wind + solar) |
| Predominant VRE | Wind (Ohio, West Virginia) |
| Dominant thermal fleet | Coal (~35%), Gas CC (~25%), Nuclear (~21%) |
| Fuel price (gas, 2024) | $2.00–$3.50/MMBtu |
| Fuel price (coal, 2024) | $55–$75/short ton |
| Average clearing price | $30–$55/MWh |

**PJM modeling notes:**
- Coal units have high `P_min` (40–50% of P_max) and slow ramps (2–5 MW/min)
- Nuclear units run as must-run baseload (`P_min ≈ 0.90 × P_max`)
- Reserve margin requirement: ~15% ICAP margin

---

## MISO (Midcontinent ISO)

| Parameter | Typical Value |
|---|---|
| Peak demand (summer) | 120,000–135,000 MW |
| Annual load factor | ~57% |
| Spinning reserve requirement | 5% of load |
| Renewable penetration (2024 actual) | ~20% (predominantly wind) |
| Predominant VRE | Wind (Great Plains — Minnesota, Iowa, Illinois) |
| Typical wind CF | 0.35–0.42 |
| Dominant thermal | Coal (~35%), Gas CC (~20%), Nuclear (~10%) |
| Fuel price (gas, 2024) | $2.00–$3.20/MMBtu |

---

## SPP (Southwest Power Pool)

| Parameter | Typical Value |
|---|---|
| Peak demand (summer) | 50,000–58,000 MW |
| Annual load factor | ~53% |
| Renewable penetration (2024 actual) | ~40% (highest wind fraction in US) |
| Predominant VRE | Wind (Kansas, Oklahoma, Texas panhandle) |
| Typical wind CF | 0.38–0.48 (excellent resource) |
| Dominant thermal | Gas CC, Gas CT, Coal |

---

## NYISO (New York ISO)

| Parameter | Typical Value |
|---|---|
| Peak demand (summer) | 32,000–34,000 MW |
| Annual load factor | ~55% |
| Renewable penetration (2024 actual) | ~28% (hydro + wind + solar) |
| Hydro capacity | ~4,800 MW (flexible) |
| Fuel price (gas, 2024) | $4.00–$7.00/MMBtu (higher due to gas pipeline constraints) |
| Average clearing price | $40–$80/MWh |

---

## Generator Type Reference

| Type | P_min / P_max | Ramp rate | Heat rate | Emission rate |
|---|---|---|---|---|
| Gas CC | 30% | 8–12 MW/min | 6.0–7.5 MMBtu/MWh | 0.33–0.45 tCO2/MWh |
| Gas CT | 20% | 3–5 MW/min | 9.0–11.5 MMBtu/MWh | 0.50–0.65 tCO2/MWh |
| Coal (subcritical) | 40% | 2–4 MW/min | 9.5–11.0 MMBtu/MWh | 0.85–1.05 tCO2/MWh |
| Nuclear | 90% | 2–5 MW/min | 10.5 MMBtu/MWh (thermal) | 0.0 tCO2/MWh |
| Solar PV | 0% | N/A (non-dispatchable) | N/A | 0.0 tCO2/MWh |
| Wind | 0% | N/A (non-dispatchable) | N/A | 0.0 tCO2/MWh |
| Battery (4h) | 0% | 100% in 15 min | N/A | 0.0 tCO2/MWh |
