#!/usr/bin/env python
"""Subprocess entry point for the pypsa_toy adapter.

    python adapters/pypsa_toy/runner.py --config <config.json> --run-dir <dir>

Reads the config, builds the toy network, solves it with HiGHS through
``n.optimize`` and writes CSV outputs into ``<run_dir>/outputs/``. Everything
it prints (its own ``[pypsa_toy] ...`` marker lines, pypsa/linopy logging and
the HiGHS console log) goes to stdout/stderr, which the adapter tees into
``solver.log``. The adapter reads the status from the marker line

    [pypsa_toy] status: OPTIMAL | INFEASIBLE | TIME_LIMIT | ERROR

Exit codes: 0 for any solver outcome (infeasible is a *result*, not an
error), 2 for a preflight (schema) failure, 1 for an unexpected exception.
Must be run with an interpreter that has pypsa + linopy + highspy.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
import traceback
from pathlib import Path
from typing import Any, Dict, List, Optional

HERE = Path(__file__).resolve()
REPO_ROOT = HERE.parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from adapters.pypsa_toy import schema  # noqa: E402
from adapters.pypsa_toy.network import (  # noqa: E402
    CO2_T_PER_MWH_TH,
    RE_CARRIERS,
    SHED_CARRIER,
    VOLL,
    build_network,
    horizon_summary,
    re_share_extra_functionality,
)

MARK = "[pypsa_toy]"

# linopy termination condition -> the framework's canonical status.
# (linopy.constants.TerminationCondition; HiGHS maps kTimeLimit -> "time_limit",
# kObjectiveBound/kObjectiveTarget/kSolutionLimit -> "terminated_by_limit".)
CONDITION_TO_STATUS = {
    "optimal": "OPTIMAL",
    "infeasible": "INFEASIBLE",
    "infeasible_or_unbounded": "INFEASIBLE",
    "time_limit": "TIME_LIMIT",
    "terminated_by_limit": "TIME_LIMIT",
    "iteration_limit": "TIME_LIMIT",
    # everything else (unbounded, internal_solver_error, unknown, ...) is a
    # solver-side error the analyzer must look at
}

# Fallback: HiGHS' own "Model   status      : <...>" line, read from highs.log
# when ``n.optimize`` raises before linopy can set a termination condition
# (e.g. a time limit hit with no primal solution to hand back to pypsa).
HIGHS_MODEL_STATUS_TO_STATUS = [
    ("optimal", "OPTIMAL"),
    ("time limit reached", "TIME_LIMIT"),
    ("iteration limit reached", "TIME_LIMIT"),
    ("solution limit reached", "TIME_LIMIT"),
    ("objective bound reached", "TIME_LIMIT"),
    ("objective target reached", "TIME_LIMIT"),
    ("infeasible", "INFEASIBLE"),
    ("primal infeasible", "INFEASIBLE"),
    ("unbounded or infeasible", "INFEASIBLE"),
]

OUTPUT_FILES = [
    "generator_results",
    "storage_results",
    "line_results",
    "cost_results",
    "emissions_results",
    "nse_results",
]


def say(msg: str) -> None:
    print(f"{MARK} {msg}", flush=True)


def _flush() -> None:
    sys.stdout.flush()
    sys.stderr.flush()


def status_from_highs_log(text: str) -> Optional[str]:
    """Canonical status from HiGHS' ``Model   status      : <...>`` line.

    Used only as a fallback when linopy never produced a termination
    condition. Returns None when the log says nothing decisive.
    """
    found: Optional[str] = None
    for raw in text.splitlines():
        line = raw.strip().lower()
        if not line.startswith("model") or "status" not in line or ":" not in line:
            continue
        value = line.split(":", 1)[1].strip()
        for token, status in HIGHS_MODEL_STATUS_TO_STATUS:
            if token in value:
                found = status  # keep the last (post-solve) model status line
                break
    return found


def preflight_notes(cfg: Dict[str, Any], hs: Dict[str, Any]) -> List[str]:
    """Warnings worth printing before the solve, given a defaults-applied config
    and its horizon facts.

    These are the analyzer's shortcut to the right diagnosis: each one names a
    *physical* limit the config has crossed, so an INFEASIBLE result downstream
    does not have to be reverse-engineered from the LP. Pure function, no I/O —
    it is unit-tested without a solver.
    """
    notes: List[str] = []
    cap = cfg["co2_cap_t"]
    if cap is not None and cap < hs["emissions_floor_t"]:
        notes.append(
            f"WARNING: co2_cap_t = {cap} t is below the must-run coal emissions floor "
            f"of {hs['emissions_floor_t']} t for this horizon; no Tier-A/B change can "
            "satisfy it — expect INFEASIBLE")
    floor = cfg["re_share_min"]
    if floor is not None:
        potential = hs["re_potential_share_of_demand"]
        if floor > potential:
            notes.append(
                f"WARNING: re_share_min = {floor} exceeds the wind + solar technical "
                f"potential for this horizon, which is {potential} of demand (600 MW "
                "wind + 800 MW solar); no Tier-A/B change can raise it — expect "
                "INFEASIBLE")
        elif floor > 0.9:
            notes.append(
                f"WARNING: re_share_min = {floor} is above the ~0.92 ceiling the "
                "must-run coal unit leaves attainable at any demand level")
    if cfg["gas_price"] == 0:
        notes.append("WARNING: gas_price = 0 — gas becomes free energy; expect an "
                     "all-gas dispatch")
    if cfg["coal_price"] == 0:
        notes.append("WARNING: coal_price = 0 — coal becomes free energy; expect an "
                     "all-coal dispatch and implausible emissions")
    if not cfg["allow_load_shedding"]:
        notes.append("note: allow_load_shedding is false, so every MWh of demand must "
                     "be served. That is a policy premise (Tier C), not a numerical "
                     "setting — an infeasible run is not a licence to turn it on.")
    return notes


# ---- outputs ----------------------------------------------------------------

def write_outputs(n, cfg: Dict[str, Any], out_dir: Path, solve_seconds: float) -> Dict[str, Any]:
    """Write the six result CSVs from a solved network; return headline numbers."""
    import numpy as np
    import pandas as pd

    out_dir.mkdir(parents=True, exist_ok=True)
    gens = n.generators
    p = n.generators_t.p.reindex(columns=gens.index).fillna(0.0)
    w = n.snapshot_weightings["generators"].reindex(n.snapshots).fillna(1.0)
    energy = p.mul(w, axis=0).sum()                         # MWh per generator
    hours = float(w.sum())

    # availability / curtailment for RE
    pmax = n.get_switchable_as_dense("Generator", "p_max_pu").reindex(columns=gens.index)
    available = pmax.mul(gens.p_nom_opt, axis=1).mul(w, axis=0).sum()
    is_re = gens.carrier.isin(RE_CARRIERS)
    curtail = (available - energy).where(is_re, 0.0).clip(lower=0.0)

    co2_factor = gens.carrier.map(n.carriers.co2_emissions).fillna(0.0)
    emissions = energy / gens.efficiency * co2_factor
    marginal = n.get_switchable_as_dense("Generator", "marginal_cost").reindex(columns=gens.index)
    variable_cost = (p * marginal).mul(w, axis=0).sum()
    build_mw = (gens.p_nom_opt - gens.p_nom).clip(lower=0.0).where(gens.p_nom_extendable, 0.0)
    gen_capital = build_mw * gens.capital_cost
    non_shed = gens.carrier != SHED_CARRIER
    total_gen = float(energy[non_shed].sum())

    gen_df = pd.DataFrame({
        "generator": gens.index,
        "bus": gens.bus.values,
        "carrier": gens.carrier.values,
        "extendable": gens.p_nom_extendable.values,
        "p_nom_existing_mw": gens.p_nom.values,
        "p_nom_opt_mw": gens.p_nom_opt.values,
        "p_nom_max_mw": gens.p_nom_max.values,
        "energy_mwh": energy.values,
        "energy_share": (energy / total_gen if total_gen > 0 else energy * 0).values,
        "capacity_factor": np.where(gens.p_nom_opt.values > 0,
                                    energy.values / np.maximum(gens.p_nom_opt.values, 1e-9) / hours, 0.0),
        "available_mwh": available.where(is_re, np.nan).values,
        "curtailment_mwh": curtail.values,
        "marginal_cost_usd_per_mwh": gens.marginal_cost.values,
        "variable_cost_usd": variable_cost.values,
        "capital_cost_usd": gen_capital.values,
        "emissions_t": emissions.values,
    })
    gen_df.to_csv(out_dir / "generator_results.csv", index=False)

    # storage
    su = n.storage_units
    if len(su):
        sp = n.storage_units_t.p.reindex(columns=su.index).fillna(0.0)
        soc = n.storage_units_t.state_of_charge.reindex(columns=su.index).fillna(0.0)
        discharged = sp.clip(lower=0.0).mul(w, axis=0).sum()
        charged = (-sp.clip(upper=0.0)).mul(w, axis=0).sum()
        e_cap = su.p_nom_opt * su.max_hours
        su_capital = (su.p_nom_opt - su.p_nom).clip(lower=0.0).where(su.p_nom_extendable, 0.0) * su.capital_cost
        st_df = pd.DataFrame({
            "storage_unit": su.index,
            "bus": su.bus.values,
            "p_nom_opt_mw": su.p_nom_opt.values,
            "energy_capacity_mwh": e_cap.values,
            "capital_cost_usd": su_capital.values,
            "discharged_mwh": discharged.values,
            "charged_mwh": charged.values,
            "full_cycles": np.where(e_cap.values > 0, discharged.values / np.maximum(e_cap.values, 1e-9), 0.0),
            "max_soc_mwh": soc.max().values,
        })
    else:
        st_df = pd.DataFrame(columns=["storage_unit", "bus", "p_nom_opt_mw", "energy_capacity_mwh",
                                      "capital_cost_usd", "discharged_mwh", "charged_mwh",
                                      "full_cycles", "max_soc_mwh"])
    st_df.to_csv(out_dir / "storage_results.csv", index=False)

    # lines
    ln = n.lines
    flow = n.lines_t.p0.reindex(columns=ln.index).fillna(0.0).abs()
    cap = ln.s_nom_opt.where(ln.s_nom_opt > 0, ln.s_nom)
    line_capital = (ln.s_nom_opt - ln.s_nom).clip(lower=0.0).where(ln.s_nom_extendable, 0.0) * ln.capital_cost
    ln_df = pd.DataFrame({
        "line": ln.index,
        "bus0": ln.bus0.values,
        "bus1": ln.bus1.values,
        "extendable": ln.s_nom_extendable.values,
        "s_nom_existing_mw": ln.s_nom.values,
        "s_nom_opt_mw": ln.s_nom_opt.values,
        "expansion_mw": (ln.s_nom_opt - ln.s_nom).clip(lower=0.0).values,
        "capital_cost_usd": line_capital.values,
        "max_abs_flow_mw": flow.max().values,
        "mean_abs_flow_mw": flow.mean().values,
        "utilisation_max": (flow.max() / cap.replace(0, np.nan)).fillna(0.0).values,
        "congested_hours": (flow >= 0.99 * cap).sum().values,
    })
    ln_df.to_csv(out_dir / "line_results.csv", index=False)

    # demand / shedding per bus
    loads = n.loads
    lp = n.loads_t.p_set.reindex(columns=loads.index).fillna(0.0)
    demand_by_bus = lp.mul(w, axis=0).sum().groupby(loads.bus).sum()
    # Column-wise grouping via transpose: DataFrame.groupby(axis=1) is
    # deprecated in pandas 2.2 and removed in 3.0.
    peak_by_bus = lp.T.groupby(loads.bus).sum().T.max()
    shed_gens = gens.index[gens.carrier == SHED_CARRIER]
    shed_by_bus = energy[shed_gens].groupby(gens.bus[shed_gens]).sum() if len(shed_gens) else pd.Series(dtype=float)
    shed_hours_by_bus = ((p[shed_gens] > 1e-6).T.groupby(gens.bus[shed_gens]).any().T.sum()
                         if len(shed_gens) else pd.Series(dtype=float))
    rows = []
    for bus in n.buses.index:
        d = float(demand_by_bus.get(bus, 0.0))
        s = float(shed_by_bus.get(bus, 0.0))
        rows.append({
            "bus": bus,
            "demand_mwh": d,
            "served_mwh": d - s,
            "shed_mwh": s,
            "shed_share": (s / d) if d > 0 else 0.0,
            "shed_hours": int(shed_hours_by_bus.get(bus, 0)) if len(shed_gens) else 0,
            "peak_demand_mw": float(peak_by_bus.get(bus, 0.0)),
        })
    d_tot = sum(r["demand_mwh"] for r in rows)
    s_tot = sum(r["shed_mwh"] for r in rows)
    rows.append({
        "bus": "total", "demand_mwh": d_tot, "served_mwh": d_tot - s_tot, "shed_mwh": s_tot,
        "shed_share": (s_tot / d_tot) if d_tot > 0 else 0.0,
        "shed_hours": int(max((r["shed_hours"] for r in rows), default=0)),
        "peak_demand_mw": float(lp.sum(axis=1).max()),
    })
    pd.DataFrame(rows).to_csv(out_dir / "nse_results.csv", index=False)

    # emissions by carrier + cap accounting
    cap_t = cfg.get("co2_cap_t")
    total_em = float(emissions.sum())
    mu = None
    if "co2_cap" in n.global_constraints.index:
        try:
            mu = float(n.global_constraints.at["co2_cap", "mu"])
        except Exception:  # pragma: no cover - defensive
            mu = None
    em_rows = []
    for carrier in n.carriers.index:
        if carrier == "AC":
            continue
        sel = gens.carrier == carrier
        e = float(energy[sel].sum())
        t = float(emissions[sel].sum())
        em_rows.append({
            "carrier": carrier,
            "energy_mwh": e,
            "emissions_t": t,
            "intensity_t_per_mwh": (t / e) if e > 0 else 0.0,
            "co2_cap_t": cap_t if cap_t is not None else "",
            "cap_utilisation": (total_em / cap_t) if cap_t else "",
            "cap_shadow_price_usd_per_t": mu if mu is not None else "",
        })
    em_rows.append({
        "carrier": "total", "energy_mwh": total_gen, "emissions_t": total_em,
        "intensity_t_per_mwh": (total_em / total_gen) if total_gen > 0 else 0.0,
        "co2_cap_t": cap_t if cap_t is not None else "",
        "cap_utilisation": (total_em / cap_t) if cap_t else "",
        "cap_shadow_price_usd_per_t": mu if mu is not None else "",
    })
    pd.DataFrame(em_rows).to_csv(out_dir / "emissions_results.csv", index=False)

    # costs
    shed_cost = float(variable_cost[shed_gens].sum()) if len(shed_gens) else 0.0
    fuel_vom = float(variable_cost[non_shed].sum())
    capital = float(gen_capital.sum() + (su_capital.sum() if len(su) else 0.0) + line_capital.sum())
    re_energy = float(energy[is_re].sum())
    # n.objective is None on a network that was never solved (pypsa logs a
    # warning and hands back None rather than raising), so coerce explicitly.
    raw_objective = getattr(n, "objective", None)
    objective = float(raw_objective) if raw_objective is not None else float("nan")
    raw_constant = getattr(n, "objective_constant", None)
    objective_constant = float(raw_constant) if raw_constant is not None else 0.0
    cost = {
        "objective_usd": objective,
        "objective_constant_usd": objective_constant,
        "capital_cost_usd": capital,
        "variable_cost_usd": fuel_vom,
        "shedding_cost_usd": shed_cost,
        "total_system_cost_usd": capital + fuel_vom + shed_cost,
        "demand_mwh": d_tot,
        "average_cost_usd_per_mwh": ((capital + fuel_vom + shed_cost) / d_tot) if d_tot > 0 else 0.0,
        "emissions_t": total_em,
        "re_share_of_generation": (re_energy / total_gen) if total_gen > 0 else 0.0,
        # Carrier shares live here too so the output-analyzer has scalar
        # anomaly metrics without joining generator_results.
        "gas_energy_share": (float(energy[gens.carrier == "gas"].sum()) / total_gen)
                            if total_gen > 0 else 0.0,
        "coal_energy_share": (float(energy[gens.carrier == "coal"].sum()) / total_gen)
                             if total_gen > 0 else 0.0,
        "shed_share_of_demand": (s_tot / d_tot) if d_tot > 0 else 0.0,
        "snapshots": int(len(n.snapshots)),
        "snapshots_days": int(cfg["snapshots_days"]),
        "solve_seconds": round(float(solve_seconds), 3),
    }
    pd.DataFrame([cost]).to_csv(out_dir / "cost_results.csv", index=False)

    headline = dict(cost)
    headline["gas_energy_share"] = float(energy[gens.carrier == "gas"].sum() / total_gen) if total_gen > 0 else 0.0
    headline["coal_energy_share"] = float(energy[gens.carrier == "coal"].sum() / total_gen) if total_gen > 0 else 0.0
    headline["wind_p_nom_opt_mw"] = float(gens.p_nom_opt.get("wind", 0.0))
    headline["solar_p_nom_opt_mw"] = float(gens.p_nom_opt.get("solar", 0.0))
    headline["gas_p_nom_opt_mw"] = float(gens.p_nom_opt.get("gas", 0.0))
    headline["coal_new_p_nom_opt_mw"] = float(gens.p_nom_opt.get("coal_new", 0.0))
    return headline


# ---- main -------------------------------------------------------------------

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="pypsa_toy runner")
    ap.add_argument("--config", required=True)
    ap.add_argument("--run-dir", required=True)
    args = ap.parse_args(argv)

    run_dir = Path(args.run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)
    out_dir = run_dir / "outputs"

    # Start clean. Re-running into an existing run dir must not leave last
    # run's CSVs (or last run's HiGHS log) behind to masquerade as this run's
    # results — an INFEASIBLE run that inherits an OPTIMAL run's outputs is the
    # worst kind of wrong answer.
    if out_dir.exists():
        shutil.rmtree(out_dir, ignore_errors=True)
    (run_dir / "highs.log").unlink(missing_ok=True)
    (run_dir / "summary.json").unlink(missing_ok=True)

    say(f"runner start (python {sys.version.split()[0]}, cwd {Path.cwd()})")
    try:
        cfg_raw = json.loads(Path(args.config).read_text())
    except Exception as exc:
        say(f"preflight error: cannot read config {args.config}: {exc}")
        say("error_origin: preflight")
        say("status: ERROR")
        return 2

    errors = schema.validate(cfg_raw)
    if errors:
        for e in errors:
            say(f"preflight error: {e}")
        say("error_origin: preflight")
        say("status: ERROR")
        return 2

    cfg = schema.apply_defaults(cfg_raw)
    say("config: " + json.dumps(cfg, sort_keys=True))
    hs = horizon_summary(cfg)
    for k, v in hs.items():
        say(f"horizon {k} = {v}")
    for note in preflight_notes(cfg, hs):
        say(note)

    summary: Dict[str, Any] = {"config": cfg, "horizon": hs}
    highs_log = run_dir / "highs.log"
    n = None
    status: Any = None
    condition: Any = None
    solve_seconds = 0.0
    t_solve = time.monotonic()
    try:
        import pypsa
        say(f"pypsa {pypsa.__version__}")
        n = build_network(cfg)
        say(f"network built: {len(n.buses)} buses, {len(n.generators)} generators, "
            f"{len(n.storage_units)} storage units, {len(n.lines)} lines, "
            f"{len(n.global_constraints)} global constraints, {len(n.snapshots)} snapshots")
        solver_options = {
            "time_limit": float(cfg["time_limit"]),
            "mip_rel_gap": float(cfg["mip_gap"]),
            "threads": int(cfg["threads"]),
            "random_seed": 0,
            "log_to_console": True,
            "output_flag": True,
            # A second copy of the HiGHS log on disk. The console copy is what
            # the adapter tees into solver.log; this one is what *we* can still
            # read if n.optimize raises before linopy sets a condition (a time
            # limit with no primal solution makes pypsa's assign_solution blow
            # up), so an aborted solve is still classified, not called ERROR.
            "log_file": str(highs_log),
        }
        extra = (re_share_extra_functionality(cfg["re_share_min"])
                 if cfg["re_share_min"] is not None else None)
        say(f"solving with {cfg['solver']} options={json.dumps(solver_options)}")
        _flush()
        t_solve = time.monotonic()
        status, condition = n.optimize(
            solver_name=str(cfg["solver"]),
            solver_options=solver_options,
            extra_functionality=extra,
        )
        solve_seconds = time.monotonic() - t_solve
        _flush()
    except Exception as exc:
        solve_seconds = time.monotonic() - t_solve
        _flush()
        traceback.print_exc(file=sys.stdout)
        _flush()
        fallback = None
        try:
            if highs_log.exists():
                fallback = status_from_highs_log(highs_log.read_text(errors="replace"))
        except OSError:
            fallback = None
        if fallback in ("TIME_LIMIT", "INFEASIBLE"):
            # The solve itself reached a real outcome; only the bookkeeping
            # afterwards failed. Report the solver's verdict, not ERROR.
            say(f"n.optimize raised {type(exc).__name__} after the solve; HiGHS' own "
                f"model status says {fallback} — reporting that")
            summary.update({
                "status": fallback,
                "linopy_status": "raised",
                "termination_condition": fallback.lower(),
                "solve_seconds": round(solve_seconds, 3),
                "exception": f"{type(exc).__name__}: {exc}",
            })
            if fallback == "INFEASIBLE":
                say("error_origin: solver")
            (run_dir / "summary.json").write_text(json.dumps(summary, indent=2, default=str))
            say(f"status: {fallback}")
            return 0
        say("error_origin: runtime")
        say("status: ERROR")
        summary.update({"status": "ERROR", "error_origin": "runtime",
                        "exception": f"{type(exc).__name__}: {exc}"})
        (run_dir / "summary.json").write_text(json.dumps(summary, indent=2, default=str))
        return 1

    canon = CONDITION_TO_STATUS.get(str(condition), "ERROR")
    say(f"linopy status: {status} / termination condition: {condition} "
        f"(solve {solve_seconds:.2f} s)")
    summary.update({
        "status": canon,
        "linopy_status": str(status),
        "termination_condition": str(condition),
        "solve_seconds": round(solve_seconds, 3),
    })

    # A solution is only attached to the network when linopy reported "ok"
    # (pypsa skips assign_solution otherwise). That covers OPTIMAL and a
    # TIME_LIMIT that still returned an incumbent — write the CSVs for both.
    if canon in ("OPTIMAL", "TIME_LIMIT") and str(status) == "ok":
        try:
            headline = write_outputs(n, cfg, out_dir, solve_seconds)
        except Exception:
            _flush()
            traceback.print_exc(file=sys.stdout)
            _flush()
            if canon == "OPTIMAL":
                say("error_origin: runtime")
                say("status: ERROR")
                summary["status"] = "ERROR"
                summary["error_origin"] = "runtime"
                (run_dir / "summary.json").write_text(json.dumps(summary, indent=2, default=str))
                return 1
            say("could not write outputs for the incumbent solution; "
                "keeping the TIME_LIMIT verdict")
            headline = None
        if headline is not None:
            summary["results"] = headline
            say(f"outputs written to {out_dir}: {', '.join(f + '.csv' for f in OUTPUT_FILES)}")
            say(f"objective = {headline['objective_usd']:.2f} USD; "
                f"total system cost = {headline['total_system_cost_usd']:.2f} USD; "
                f"emissions = {headline['emissions_t']:.1f} t; "
                f"RE share = {headline['re_share_of_generation']:.3f}; "
                f"gas share = {headline['gas_energy_share']:.3f}; "
                f"shed share = {headline['shed_share_of_demand']:.4f}")
        if canon == "TIME_LIMIT":
            say(f"solver stopped at time_limit = {cfg['time_limit']} s without proving "
                "optimality; the numbers above are an incumbent, not the optimum")
    elif canon == "INFEASIBLE":
        say("error_origin: solver")
        say("the LP has no feasible point — see the horizon facts above and the "
            "policy keys (co2_cap_t, re_share_min, allow_load_shedding) and "
            "network limits (line_expansion_allowed, demand_scale)")
    elif canon == "TIME_LIMIT":
        say(f"solver stopped at time_limit = {cfg['time_limit']} s without proving optimality")
    else:
        say("error_origin: solver")

    (run_dir / "summary.json").write_text(json.dumps(summary, indent=2, default=str))
    say(f"status: {canon}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
