"""Deterministic 3-bus toy capacity-expansion network for the pypsa_toy adapter.

Topology (a triangle):

    north (500 MW peak; existing 300 MW must-run coal, new coal, gas)
      |  \\
      |   \\  north-south 400 MW
      |    \\
    east ---- south
    (300 MW peak; wind      (200 MW peak; solar <= 800 MW, battery)
     <= 600 MW, battery)     east-south 200 MW
    north-east 400 MW

Everything is synthetic and seeded, so a given config always builds the same
LP. The profile generator needs only numpy; pandas/pypsa are imported lazily
inside :func:`build_network` so :func:`horizon_summary` (used by
``describe_config``) works in an interpreter without pypsa.

Design facts that matter for the guardrail fixtures (see the fixtures README):

- ``coal_existing`` is must-run at 20 % of 300 MW. Its emissions
  (60 MW x hours / 0.38 x 0.34 t/MWh_th ~ 1289 t/day) are a hard floor on
  system CO2 that no Tier-A/B key can lower -> a ``co2_cap_t`` below the floor
  is infeasible for every A/B change (Tier-C-only fix).
- Wind/solar technical potential is capped (600/800 MW), which sums to ~56 % of
  default demand over the full 14 days. With the must-run coal share on top, an
  RE-share floor >= 0.9 is unattainable at any demand level.
- With ``line_expansion_allowed=false`` the east/south buses can import at
  most 600/600 MW; scaling demand x10 is then infeasible, but fixable by a
  Tier-B change (allow line expansion or lower ``demand_scale``).
"""

from __future__ import annotations

from typing import Any, Callable, Dict

import numpy as np

from adapters.pypsa_toy.schema import MAX_DAYS, apply_defaults

# ---- fixed physical/technology assumptions (NOT config keys) ---------------

SEED = 20300107
START = "2030-01-07"  # a Monday, so day-of-week effects line up
HOURS_PER_YEAR = 8760.0

BUSES = ["north", "east", "south"]
PEAK_MW = {"north": 500.0, "east": 300.0, "south": 200.0}

COAL_EXISTING_MW = 300.0
COAL_EXISTING_MIN_PU = 0.20         # must-run floor
COAL_EXISTING_EFF = 0.38
COAL_NEW_EFF = 0.42
COAL_NEW_MAX_MW = 1000.0
COAL_NEW_CAPEX = 150_000.0          # $/MW/yr annualised
COAL_VOM = 3.0                      # $/MWh_el

GAS_EFF = 0.55
GAS_CAPEX = 55_000.0
GAS_VOM = 2.0
GAS_MAX_MW = float("inf")           # gas build is unlimited (at the north bus)

CO2_T_PER_MWH_TH = {"coal": 0.34, "gas": 0.20}

WIND_MAX_MW = 600.0                 # technical potential at east
SOLAR_MAX_MW = 800.0                # technical potential at south
BATTERY_MAX_MW = 500.0              # per battery (east and south)
BATTERY_HOURS = 4.0
BATTERY_EFF = 0.95                  # each way -> 0.9 round trip

LINES = {
    # name: (bus0, bus1, existing s_nom MW)
    "north-east": ("north", "east", 400.0),
    "north-south": ("north", "south", 400.0),
    "east-south": ("east", "south", 200.0),
}
LINE_CAPEX = 20_000.0               # $/MW/yr annualised
LINE_MAX_MW = 20_000.0
LINE_X = 0.1
LINE_R = 0.01

VOLL = 10_000.0                     # $/MWh load-shedding price
SHED_MW = 1e5                       # effectively unlimited shedding capacity

RE_CARRIERS = ["wind", "solar"]
SHED_CARRIER = "load_shedding"


# ---- synthetic profiles -----------------------------------------------------

def make_profiles(n_days: int = MAX_DAYS) -> Dict[str, Any]:
    """Seeded hourly profiles for the full 14-day series, sliced to ``n_days``.

    Returns dict with ``hours``, ``demand`` ({bus: MW array at demand_scale 1}),
    ``wind`` (per-unit), ``solar`` (per-unit).
    """
    n_days = int(n_days)
    if not 1 <= n_days <= MAX_DAYS:
        raise ValueError(f"n_days must be in 1..{MAX_DAYS}, got {n_days}")

    rng = np.random.default_rng(SEED)
    H = MAX_DAYS * 24
    t = np.arange(H)
    hod = t % 24
    dow = (t // 24) % 7

    # Demand: evening peak + morning shoulder, weekend dip, mild noise.
    evening = np.exp(-((hod - 18.5) / 3.5) ** 2)
    morning = 0.45 * np.exp(-((hod - 9.0) / 3.0) ** 2)
    shape = evening + morning
    shape = shape / shape.max()
    daily = 0.60 + 0.40 * shape
    weekend = np.where(dow >= 5, 0.88, 1.0)
    system_pu = np.clip(daily * weekend * (1 + 0.03 * rng.standard_normal(H)), 0.45, 1.05)
    demand = {}
    for bus in BUSES:
        local_noise = 1 + 0.02 * rng.standard_normal(H)
        demand[bus] = np.clip(PEAK_MW[bus] * system_pu * local_noise, 0.0, None)

    # Wind: AR(1) around 0.35 with a nightly boost.
    eps = rng.standard_normal(H)
    w = np.empty(H)
    w[0] = 0.35
    for i in range(1, H):
        w[i] = 0.35 + 0.92 * (w[i - 1] - 0.35) + 0.10 * eps[i]
    w = w + 0.06 * np.cos(2 * np.pi * (hod - 3) / 24.0)
    wind = np.clip(w, 0.02, 0.95)

    # Solar: clear-sky bell (daylight ~06:30-18:30) x a daily cloud factor.
    clear_sky = np.maximum(0.0, np.cos(np.pi * (hod - 12.5) / 12.0)) ** 1.4 * 0.85
    cloud = rng.uniform(0.45, 1.0, MAX_DAYS)[t // 24]
    solar = np.clip(clear_sky * cloud * (1 + 0.05 * rng.standard_normal(H)), 0.0, 1.0)

    h = n_days * 24
    return {
        "hours": h,
        "demand": {b: demand[b][:h].copy() for b in BUSES},
        "wind": wind[:h].copy(),
        "solar": solar[:h].copy(),
    }


def horizon_summary(config: Dict[str, Any]) -> Dict[str, float]:
    """Analytic facts about a config's horizon (no pypsa needed): demand energy,
    RE potential, the must-run emissions floor and an all-coal ceiling. Used by
    ``describe_config`` and printed into the solver log so an analyzer can
    judge whether a cap is physically attainable."""
    cfg = apply_defaults(config)
    n_days = int(cfg["snapshots_days"])
    prof = make_profiles(n_days)
    hours = prof["hours"]
    scale = float(cfg["demand_scale"])
    demand_mwh = float(sum(prof["demand"][b].sum() for b in BUSES)) * scale
    peak_mw = float(sum(prof["demand"][b] for b in BUSES).max()) * scale
    wind_mwh = float(prof["wind"].sum()) * WIND_MAX_MW
    solar_mwh = float(prof["solar"].sum()) * SOLAR_MAX_MW
    floor_mwh_el = COAL_EXISTING_MW * COAL_EXISTING_MIN_PU * hours
    floor_t = floor_mwh_el / COAL_EXISTING_EFF * CO2_T_PER_MWH_TH["coal"]
    ceiling_t = demand_mwh / COAL_EXISTING_EFF * CO2_T_PER_MWH_TH["coal"]
    return {
        "snapshots_days": n_days,
        "hours": hours,
        "demand_mwh": round(demand_mwh, 1),
        "peak_demand_mw": round(peak_mw, 1),
        "wind_potential_mwh": round(wind_mwh, 1),
        "solar_potential_mwh": round(solar_mwh, 1),
        "re_potential_share_of_demand": round((wind_mwh + solar_mwh) / demand_mwh, 3),
        "must_run_coal_mwh": round(floor_mwh_el, 1),
        "emissions_floor_t": round(floor_t, 1),
        "emissions_all_coal_ceiling_t": round(ceiling_t, 1),
    }


# ---- network construction ---------------------------------------------------

def build_network(config: Dict[str, Any]):
    """Build the pypsa.Network for a (defaults-applied) config. Deterministic."""
    import pandas as pd  # lazy: keep this module importable without pandas
    import pypsa

    cfg = apply_defaults(config)
    n_days = int(cfg["snapshots_days"])
    hours = n_days * 24
    hf = hours / HOURS_PER_YEAR  # pro-rate annualised capital costs to the horizon
    prof = make_profiles(n_days)

    n = pypsa.Network(name="pypsa_toy")
    n.set_snapshots(pd.date_range(START, periods=hours, freq="h"))
    idx = n.snapshots

    n.add(
        "Carrier",
        ["AC", "coal", "gas", "wind", "solar", "battery", SHED_CARRIER],
        co2_emissions=[0.0, CO2_T_PER_MWH_TH["coal"], CO2_T_PER_MWH_TH["gas"], 0.0, 0.0, 0.0, 0.0],
    )
    n.add("Bus", BUSES, v_nom=380.0, carrier="AC")

    scale = float(cfg["demand_scale"])
    for bus in BUSES:
        n.add("Load", f"load_{bus}", bus=bus, carrier="AC",
              p_set=pd.Series(scale * prof["demand"][bus], index=idx))

    coal_price = float(cfg["coal_price"])
    gas_price = float(cfg["gas_price"])

    n.add("Generator", "coal_existing", bus="north", carrier="coal",
          p_nom=COAL_EXISTING_MW, p_nom_extendable=False,
          p_min_pu=COAL_EXISTING_MIN_PU, efficiency=COAL_EXISTING_EFF,
          marginal_cost=coal_price / COAL_EXISTING_EFF + COAL_VOM)
    n.add("Generator", "coal_new", bus="north", carrier="coal",
          p_nom_extendable=True, p_nom_max=COAL_NEW_MAX_MW,
          efficiency=COAL_NEW_EFF, capital_cost=COAL_NEW_CAPEX * hf,
          marginal_cost=coal_price / COAL_NEW_EFF + COAL_VOM)
    n.add("Generator", "gas", bus="north", carrier="gas",
          p_nom_extendable=True, p_nom_max=GAS_MAX_MW,
          efficiency=GAS_EFF, capital_cost=GAS_CAPEX * hf,
          marginal_cost=gas_price / GAS_EFF + GAS_VOM)
    n.add("Generator", "wind", bus="east", carrier="wind",
          p_nom_extendable=True, p_nom_max=WIND_MAX_MW,
          capital_cost=float(cfg["wind_capex"]) * hf, marginal_cost=0.0,
          p_max_pu=pd.Series(prof["wind"], index=idx))
    n.add("Generator", "solar", bus="south", carrier="solar",
          p_nom_extendable=True, p_nom_max=SOLAR_MAX_MW,
          capital_cost=float(cfg["solar_capex"]) * hf, marginal_cost=0.0,
          p_max_pu=pd.Series(prof["solar"], index=idx))

    if bool(cfg["allow_load_shedding"]):
        for bus in BUSES:
            n.add("Generator", f"shed_{bus}", bus=bus, carrier=SHED_CARRIER,
                  p_nom=SHED_MW, p_nom_extendable=False, marginal_cost=VOLL)

    for bus in ("east", "south"):
        n.add("StorageUnit", f"battery_{bus}", bus=bus, carrier="battery",
              p_nom_extendable=True, p_nom_max=BATTERY_MAX_MW,
              max_hours=BATTERY_HOURS,
              efficiency_store=BATTERY_EFF, efficiency_dispatch=BATTERY_EFF,
              cyclic_state_of_charge=True,
              capital_cost=float(cfg["battery_capex"]) * hf)

    ext = bool(cfg["line_expansion_allowed"])
    for name, (b0, b1, s_nom) in LINES.items():
        n.add("Line", name, bus0=b0, bus1=b1, carrier="AC",
              s_nom=s_nom, s_nom_extendable=ext,
              s_nom_min=s_nom if ext else 0.0,
              s_nom_max=LINE_MAX_MW if ext else s_nom,
              capital_cost=LINE_CAPEX * hf if ext else 0.0,
              x=LINE_X, r=LINE_R)

    if cfg["co2_cap_t"] is not None:
        n.add("GlobalConstraint", "co2_cap", type="primary_energy",
              carrier_attribute="co2_emissions", sense="<=",
              constant=float(cfg["co2_cap_t"]))

    n.meta = {"adapter": "pypsa_toy", "config": cfg, "horizon": horizon_summary(cfg)}
    return n


def re_share_extra_functionality(share: float) -> Callable:
    """``extra_functionality`` hook adding the RE-share floor as a linopy
    constraint: sum(wind+solar p) >= share * sum(all non-shedding p)."""
    share = float(share)

    def add_re_share(n, snapshots) -> None:
        m = n.model
        p = m.variables["Generator-p"]
        gens = n.generators
        re_names = list(gens.index[gens.carrier.isin(RE_CARRIERS)])
        tot_names = list(gens.index[gens.carrier != SHED_CARRIER])
        lhs = p.loc[:, re_names].sum() - share * p.loc[:, tot_names].sum()
        m.add_constraints(lhs >= 0, name="re_share_min")

    return add_re_share
