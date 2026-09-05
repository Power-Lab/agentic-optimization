#!/usr/bin/env python3
"""Smoke test: drive the model's own CI regression case through GarudaAdapter.

Runs maluku 2030 base dispatch (HiGHS, LP-relaxed unit commitment — the case
garuda's CI re-solves on every push) live and checks:

  1. a ``solver.log`` was captured,
  2. the run terminated OPTIMAL,
  3. the three headline numbers match the model's reference values within
     +-1 %: ``cost_results.Total_Costs``, ``clean_energy_results.CO2_Emissions``
     and the zone sum of ``reliability_results.Total_NSE_MWh``.

No licence needed; minutes on HiGHS. ``--solver gurobi`` re-runs the same LP on
Gurobi (same headlines expected). The 12.4 % unserved energy on this dataset is
a real reliability gap, not a defect.

Usage:
    python examples/garuda/smoke_test.py [--solver highs|gurobi] [--bootstrap]
                                         [--run-dir DIR] [--tol 0.01]
                                         [--skip-schema-validation]
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from adapters.garuda import (  # noqa: E402
    GarudaAdapter,
    REGRESSION_CONFIG,
    REGRESSION_TOLERANCE,
    check_regression_headlines,
)
from framework import run_and_record  # noqa: E402

FRAMEWORK_ROOT = Path(__file__).resolve().parents[2]
MODEL_ROOT = FRAMEWORK_ROOT / "models" / "garuda"
# Not runs/smoke_base_maluku: that directory holds a Gurobi MILP log other tests
# replay, and a fresh HiGHS dispatch log must not overwrite it.
DEFAULT_RUN_DIR = FRAMEWORK_ROOT / "runs" / "smoke_garuda_maluku_dispatch"


def bootstrap() -> None:
    """One-time Julia environment instantiation (the orchestrator normally
    does this; do not run it while another bootstrap is in progress)."""
    print("[smoke] Bootstrapping Julia environment (one-time)…")
    proc = subprocess.run(
        ["julia", f"--project={MODEL_ROOT}", "bootstrap.jl"], cwd=MODEL_ROOT,
    )
    if proc.returncode != 0:
        sys.exit("[smoke] FAIL: Julia bootstrap failed.")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--solver", choices=["highs", "gurobi"], default="highs")
    ap.add_argument("--bootstrap", action="store_true",
                    help="run julia bootstrap.jl first (normally already done)")
    ap.add_argument("--run-dir", default=str(DEFAULT_RUN_DIR))
    ap.add_argument("--tol", type=float, default=REGRESSION_TOLERANCE,
                    help="relative tolerance on the headline numbers")
    ap.add_argument("--skip-schema-validation", action="store_true",
                    help="set GARUDA_SKIP_VALIDATION=1 for the model's preflight")
    args = ap.parse_args()

    if not (MODEL_ROOT / "run_model.jl").exists():
        sys.exit(f"[smoke] FAIL: model not found at {MODEL_ROOT}. Init the submodule.")
    if args.bootstrap:
        bootstrap()

    config = dict(REGRESSION_CONFIG)
    config["solver"] = args.solver
    adapter = GarudaAdapter(model_root=MODEL_ROOT,
                            skip_schema_validation=args.skip_schema_validation)
    run_dir = Path(args.run_dir)

    print(f"[smoke] adapter = {adapter.name}; validator python = {adapter.python_for_validator}")
    print(f"[smoke] running {adapter.results_name(config)} on {args.solver} → {run_dir}")
    record = run_and_record(adapter, config, run_dir, preflight=True)

    ok = True

    # Check 1: solver.log captured (streamed line by line during the solve).
    log = run_dir / "solver.log"
    if log.exists() and log.stat().st_size > 0:
        print(f"[smoke] PASS: solver.log captured ({log.stat().st_size} bytes)")
    else:
        ok = False
        print("[smoke] FAIL: solver.log missing or empty")

    # Check 2: terminated OPTIMAL.
    ex = record.execution
    if ex.termination_status == "OPTIMAL":
        print(f"[smoke] PASS: termination_status = OPTIMAL ({ex.wall_seconds}s, rc={ex.returncode})")
    else:
        ok = False
        print(f"[smoke] FAIL: termination_status = {ex.termination_status} "
              f"(origin={ex.error_origin}, rc={ex.returncode})")
        if log.exists():
            print(log.read_text()[-2000:])

    # Check 3: the three headline numbers within tolerance.
    outputs = adapter.locate_outputs(run_dir)
    if not outputs:
        ok = False
        print("[smoke] FAIL: no output CSVs archived under", run_dir / "outputs")
    else:
        report = check_regression_headlines(outputs, args.tol)
        for metric, (got, want, within) in report.items():
            if got is None:
                print(f"[smoke] FAIL: {metric} missing from outputs")
            else:
                rel = abs(got - want) / abs(want)
                print(f"[smoke] {'PASS' if within else 'FAIL'}: {metric} = {got:.6g} "
                      f"(Δ {100 * rel:.3f} % of {want:.6g}, tol {100 * args.tol:g} %)")
            ok = ok and within
        print(f"[smoke] archived {len(outputs)} CSVs: {', '.join(sorted(outputs))}")

    print("\n[smoke]", "ALL CHECKS PASSED" if ok else "SMOKE TEST FAILED")
    print("[smoke] run record:", run_dir / "run_record.json")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
