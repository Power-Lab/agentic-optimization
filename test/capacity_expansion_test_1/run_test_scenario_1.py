from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from pathlib import Path

import pandas as pd
import yaml
from scipy.optimize import linprog
from scipy.sparse import coo_matrix, lil_matrix, vstack


@dataclass(frozen=True)
class ScenarioPaths:
    repo_root: Path
    config_path: Path
    data_dir: Path


def load_yaml(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle)
    if not isinstance(data, dict):
        raise ValueError(f"Expected YAML mapping in {path}")
    return data


def resolve_repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def read_inputs(paths: ScenarioPaths, config: dict) -> tuple[pd.DataFrame, pd.DataFrame]:
    generator_path = paths.data_dir / "generators_for_expansion.csv"
    demand_path = paths.data_dir / "demand_for_expansion.csv"

    if not generator_path.exists():
        raise FileNotFoundError(f"Missing generator data: {generator_path}")
    if not demand_path.exists():
        raise FileNotFoundError(f"Missing demand data: {demand_path}")

    generators = pd.read_csv(generator_path)
    demand = pd.read_csv(demand_path)

    required_generator_columns = {"G", "Description", "FixedCost", "VarCost"}
    required_demand_columns = {"Hour", "Demand"}
    missing_generators = required_generator_columns - set(generators.columns)
    missing_demand = required_demand_columns - set(demand.columns)
    if missing_generators:
        raise ValueError(f"Generator CSV missing columns: {sorted(missing_generators)}")
    if missing_demand:
        raise ValueError(f"Demand CSV missing columns: {sorted(missing_demand)}")

    expected_hours = int(config["sets"]["hours"]["expected_count"])
    if len(demand) != expected_hours:
        raise ValueError(f"Expected {expected_hours} demand rows, found {len(demand)}")

    include = set(config["candidate_generator_filter"]["include_values"])
    excluded = set(config["candidate_generator_filter"]["reject_if_present_after_filter"])
    thermal = generators[generators["Description"].isin(include) | generators["G"].isin(include)].copy()

    if thermal.empty:
        raise ValueError(f"No candidate generators matched include set: {sorted(include)}")
    if set(thermal["Description"]).intersection(excluded) or set(thermal["G"]).intersection(excluded):
        raise ValueError("Wind or Solar survived the thermal-only candidate filter")

    thermal["technology"] = thermal["Description"]
    thermal["technology_code"] = thermal["G"]
    thermal["fixed_cost_usd_per_mw_year"] = pd.to_numeric(thermal["FixedCost"], errors="raise")
    thermal["variable_cost_usd_per_mwh"] = pd.to_numeric(thermal["VarCost"], errors="raise")

    demand["hour"] = pd.to_numeric(demand["Hour"], errors="raise").astype(int)
    demand["demand_mw"] = pd.to_numeric(demand["Demand"], errors="raise")

    return thermal.reset_index(drop=True), demand[["hour", "demand_mw"]]


def build_and_solve_lp(generators: pd.DataFrame, demand: pd.DataFrame, config: dict):
    n_gen = len(generators)
    n_hours = len(demand)
    timestep_hours = float(config["key_parameters"]["timestep_hours"])
    nse_cost = float(config["key_parameters"]["nse_cost_usd_per_mwh"])

    build_start = 0
    gen_start = n_gen
    nse_start = gen_start + n_gen * n_hours
    n_vars = nse_start + n_hours

    objective = [0.0] * n_vars
    for g, row in generators.iterrows():
        objective[build_start + g] = row["fixed_cost_usd_per_mw_year"]
        for t in range(n_hours):
            objective[gen_start + g * n_hours + t] = row["variable_cost_usd_per_mwh"]
    for t in range(n_hours):
        objective[nse_start + t] = nse_cost

    balance = lil_matrix((n_hours, n_vars))
    rhs_balance = demand["demand_mw"].to_numpy(dtype=float) * timestep_hours
    for t in range(n_hours):
        for g in range(n_gen):
            balance[t, gen_start + g * n_hours + t] = 1.0
        balance[t, nse_start + t] = 1.0

    capacity_rows = []
    capacity_cols = []
    capacity_data = []
    for g in range(n_gen):
        for t in range(n_hours):
            row = g * n_hours + t
            capacity_rows.extend([row, row])
            capacity_cols.extend([gen_start + g * n_hours + t, build_start + g])
            capacity_data.extend([1.0, -timestep_hours])
    capacity = coo_matrix(
        (capacity_data, (capacity_rows, capacity_cols)),
        shape=(n_gen * n_hours, n_vars),
    )

    nse_limit = lil_matrix((n_hours, n_vars))
    for t in range(n_hours):
        nse_limit[t, nse_start + t] = 1.0

    a_ub = vstack([capacity, nse_limit.tocsr()], format="csr")
    b_ub = list([0.0] * (n_gen * n_hours)) + list(rhs_balance)

    result = linprog(
        c=objective,
        A_ub=a_ub,
        b_ub=b_ub,
        A_eq=balance.tocsr(),
        b_eq=rhs_balance,
        bounds=(0, None),
        method="highs",
        options={
            "time_limit": float(config["solver_settings"]["time_limit_seconds"]),
            "presolve": config["solver_settings"].get("presolve", "on") == "on",
        },
    )

    if not result.success:
        raise RuntimeError(f"HiGHS solve failed: {result.message}")

    return result, build_start, gen_start, nse_start


def write_outputs(
    paths: ScenarioPaths,
    config: dict,
    generators: pd.DataFrame,
    demand: pd.DataFrame,
    result,
    build_start: int,
    gen_start: int,
    nse_start: int,
) -> None:
    n_gen = len(generators)
    n_hours = len(demand)
    x = result.x

    output_specs = config["expected_output_files"]

    capacity_rows = []
    for g, row in generators.iterrows():
        build_mw = float(x[build_start + g])
        capacity_rows.append(
            {
                "technology": row["technology"],
                "build_capacity_mw": build_mw,
                "fixed_cost_usd": build_mw * float(row["fixed_cost_usd_per_mw_year"]),
            }
        )
    capacity_df = pd.DataFrame(capacity_rows)

    dispatch_rows = []
    for g, row in generators.iterrows():
        for t, demand_row in demand.iterrows():
            dispatch_rows.append(
                {
                    "hour": int(demand_row["hour"]),
                    "technology": row["technology"],
                    "generation_mwh": float(x[gen_start + g * n_hours + t]),
                }
            )
    dispatch_df = pd.DataFrame(dispatch_rows)

    nse_df = pd.DataFrame(
        {
            "hour": demand["hour"].astype(int),
            "non_served_energy_mwh": [float(x[nse_start + t]) for t in range(n_hours)],
        }
    )
    nse_cost = float(config["key_parameters"]["nse_cost_usd_per_mwh"])
    nse_df["nse_penalty_usd"] = nse_df["non_served_energy_mwh"] * nse_cost

    fixed_cost = float(capacity_df["fixed_cost_usd"].sum())
    variable_cost = float(
        dispatch_df.merge(
            generators[["technology", "variable_cost_usd_per_mwh"]],
            on="technology",
            how="left",
        )
        .assign(variable_cost_usd=lambda df: df["generation_mwh"] * df["variable_cost_usd_per_mwh"])
        ["variable_cost_usd"]
        .sum()
    )
    total_nse = float(nse_df["non_served_energy_mwh"].sum())
    total_nse_penalty = float(nse_df["nse_penalty_usd"].sum())

    summary = {
        "scenario_id": config["scenario_id"],
        "termination_status": "OPTIMAL",
        "solver_message": result.message,
        "objective_value_usd": float(result.fun),
        "total_fixed_cost_usd": fixed_cost,
        "total_variable_cost_usd": variable_cost,
        "total_nse_mwh": total_nse,
        "total_nse_penalty_usd": total_nse_penalty,
        "candidate_technologies": generators["technology"].tolist(),
        "hours": n_hours,
    }

    write_csv(paths.repo_root / output_specs["capacity_build"]["path"], capacity_df)
    write_csv(paths.repo_root / output_specs["hourly_dispatch"]["path"], dispatch_df)
    write_csv(paths.repo_root / output_specs["non_served_energy"]["path"], nse_df)
    write_json(paths.repo_root / output_specs["solve_summary"]["path"], summary)


def write_csv(path: Path, frame: pd.DataFrame) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    frame.to_csv(path, index=False)


def write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def parse_args() -> argparse.Namespace:
    repo_root = resolve_repo_root()
    parser = argparse.ArgumentParser(
        description="Run the thermal-only greenfield capacity expansion scenario."
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=repo_root / "test" / "scenario_builder" / "test_scenario_1_output.yaml",
    )
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=repo_root / "doc" / "expansion_data",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    repo_root = resolve_repo_root()
    paths = ScenarioPaths(
        repo_root=repo_root,
        config_path=args.config.resolve(),
        data_dir=args.data_dir.resolve(),
    )
    config = load_yaml(paths.config_path)
    generators, demand = read_inputs(paths, config)
    result, build_start, gen_start, nse_start = build_and_solve_lp(generators, demand, config)
    write_outputs(paths, config, generators, demand, result, build_start, gen_start, nse_start)
    print(f"Solved {config['scenario_id']} with objective {result.fun:.2f} USD")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
