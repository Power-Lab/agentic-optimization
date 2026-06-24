"""
validate_config.py
-------------------
Validates a scenario_config YAML file against required fields,
numeric bounds, and feasibility preconditions.

CLI usage:
    python skill_library/scenario_builder/scripts/validate_config.py \
        --config runs/run_001/scenario_config.yaml

Exits 0 if PASS, 1 if WARN, 2 if FAIL.
"""

import argparse
import sys
from pathlib import Path

import yaml


REQUIRED_TOP_KEYS = ["run_id", "region", "model_type", "time_horizon", "generators",
                     "policy_constraints", "solver_settings"]
REQUIRED_GEN_KEYS = ["id", "type", "capacity_MW"]
THERMAL_TYPES = {"gas_combined_cycle", "gas_combustion_turbine", "coal", "nuclear"}
VRE_TYPES = {"solar", "wind"}


def validate(config: dict) -> tuple[str, list[str], list[str]]:
    """
    Returns (status, warnings, errors).
    status: PASS | WARN | FAIL
    """
    warnings: list[str] = []
    errors: list[str] = []

    # --- Required top-level keys ---
    for key in REQUIRED_TOP_KEYS:
        if key not in config:
            errors.append(f"MISSING_KEY: '{key}' is required at top level.")

    if errors:
        return "FAIL", warnings, errors

    # --- Time horizon ---
    th = config.get("time_horizon", {})
    T = th.get("T", 0)
    if T <= 0:
        errors.append(f"INVALID_T: time_horizon.T must be > 0, got {T}.")
    if th.get("dt_h", 0) <= 0:
        errors.append("INVALID_DT: time_horizon.dt_h must be > 0.")

    # --- Generators ---
    gens = config.get("generators", [])
    if not gens:
        errors.append("NO_GENERATORS: At least one generator is required.")

    gen_ids = [g.get("id") for g in gens]
    if len(gen_ids) != len(set(gen_ids)):
        errors.append(f"DUPLICATE_GENERATOR_IDS: {gen_ids}")

    total_pmax = 0.0
    has_thermal = False
    for g in gens:
        for key in REQUIRED_GEN_KEYS:
            if key not in g:
                errors.append(f"GENERATOR '{g.get('id', '?')}': missing required key '{key}'.")
        cap = g.get("capacity_MW", 0)
        if cap <= 0:
            errors.append(f"GENERATOR '{g.get('id')}': capacity_MW must be > 0, got {cap}.")
        total_pmax += cap
        g_type = g.get("type", "")
        if g_type in THERMAL_TYPES:
            has_thermal = True
            p_min = g.get("P_min_MW", 0)
            if p_min <= 0:
                warnings.append(f"GENERATOR '{g.get('id')}': P_min_MW not set or 0. Recommend setting ≥ 10% of capacity.")
            ramp = g.get("ramp_up_MW_h", None)
            if ramp is None:
                warnings.append(f"GENERATOR '{g.get('id')}': ramp_up_MW_h not specified.")

    # --- Demand capacity check ---
    demand_peak = config.get("demand_assumptions", {}).get("peak_MW", 0)
    if demand_peak <= 0:
        warnings.append("demand_assumptions.peak_MW not set or 0.")
    elif total_pmax < demand_peak * 1.05:
        errors.append(
            f"INSUFFICIENT_CAPACITY: sum(P_max)={total_pmax:.0f} MW < "
            f"peak_demand * 1.05 = {demand_peak * 1.05:.0f} MW."
        )

    # --- Policy constraints ---
    pc = config.get("policy_constraints", {})
    cap_co2 = pc.get("emission_cap_tCO2")
    if cap_co2 is not None and cap_co2 <= 0:
        errors.append(f"INVALID_EMISSION_CAP: emission_cap_tCO2 must be > 0, got {cap_co2}.")

    ren_min = pc.get("renewable_penetration_min")
    if ren_min is not None:
        if not (0.0 < ren_min <= 1.0):
            errors.append(f"INVALID_RENEWABLE_MIN: must be in (0,1], got {ren_min}.")
        ren_cap = sum(g.get("capacity_MW", 0) for g in gens if g.get("type") in VRE_TYPES)
        if ren_cap == 0:
            errors.append("INFEASIBLE_PRECONDITION: renewable_penetration_min > 0 but no VRE generators found.")

    # --- Solver settings ---
    ss = config.get("solver_settings", {})
    mip_gap = ss.get("mip_gap", 0)
    if not (0 < mip_gap < 1):
        warnings.append(f"mip_gap={mip_gap} is outside typical range (0, 1).")
    time_limit = ss.get("time_limit_s", 0)
    if time_limit <= 0:
        warnings.append(f"time_limit_s={time_limit} — solver may run indefinitely.")

    status = "FAIL" if errors else ("WARN" if warnings else "PASS")
    return status, warnings, errors


def main() -> None:
    parser = argparse.ArgumentParser(description="Validate a scenario_config YAML file.")
    parser.add_argument("--config", required=True, help="Path to scenario_config.yaml.")
    args = parser.parse_args()

    config_path = Path(args.config)
    if not config_path.exists():
        print(f"[validate_config] ERROR: File not found: {config_path}")
        sys.exit(2)

    with open(config_path) as f:
        config = yaml.safe_load(f)

    status, warnings, errors = validate(config)

    print(f"[validate_config] Status: {status}")
    for w in warnings:
        print(f"  WARN: {w}")
    for e in errors:
        print(f"  ERROR: {e}")

    if status == "FAIL":
        sys.exit(2)
    elif status == "WARN":
        sys.exit(1)
    else:
        sys.exit(0)


if __name__ == "__main__":
    main()
