#!/usr/bin/env python3
"""Smoke test: drive one short single-year Pathways run through PathwaysAdapter.

Solves ``adapters.pathways.SMOKE_CONFIG`` — 2060 under the 2C emission target
with CCS available from 2040, three representative days sampled 30 days apart —
and checks:

  1. a ``solver.log`` was captured and the driver reached ``stage: done``,
  2. the run terminated OPTIMAL,
  3. the expected output CSVs were archived into ``<run_dir>/outputs/``
     (``objValue``, ``emissionValue``, ``emissionBreakdowns``,
     ``summary_national``, ``summary_provincial``),
  4. the realised emissions in ``emissionValue.csv`` are at or below the cap the
     model reports on the same row set — i.e. the policy constraint the study
     rests on actually bound.

**This was NOT executed in the build that created it.** Pathways needs a Gurobi
licence and the ~10 GB Zenodo data set, and even a three-day run is minutes of
solve time. Treat a first green run of this script as the real acceptance test
for the adapter, and read ``examples/pathways/README.md`` for the setup.

Prerequisites:
    export PATHWAYS_DATA_ROOT=/path/to/AdvAppliedEnergy_Pathways_2025
    export PATHWAYS_PYTHON=~/miniforge3/envs/agentic-pathways/bin/python
    # a valid Gurobi licence (~/gurobi.lic or GRB_LICENSE_FILE)

Usage:
    python examples/pathways/smoke_test.py [--run-dir DIR] [--days N] [--year Y]
                                           [--time-limit S] [--threads N]
                                           [--keep-workspace] [--dry-run]
"""

from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from adapters.pathways import PathwaysAdapter, SMOKE_CONFIG  # noqa: E402
from framework import run_and_record  # noqa: E402

FRAMEWORK_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_RUN_DIR = FRAMEWORK_ROOT / "runs" / "smoke_pathways_2060_3day"
EXPECTED_OUTPUTS = ["objValue", "emissionValue", "emissionBreakdowns",
                    "summary_national", "summary_provincial"]


def read_pairs(path: Path) -> dict:
    """A ``key,value[,...]`` CSV as a dict of the first two columns."""
    out = {}
    with open(path, newline="") as fh:
        for row in csv.reader(fh):
            if len(row) >= 2 and row[0].strip():
                out[row[0].strip()] = row[1].strip()
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--run-dir", default=str(DEFAULT_RUN_DIR))
    ap.add_argument("--year", type=int, default=SMOKE_CONFIG["year"])
    ap.add_argument("--days", type=int, default=SMOKE_CONFIG["optimization_days"],
                    help="representative days sampled from the 8760-hour year")
    ap.add_argument("--time-limit", type=float, default=None, help="Gurobi TimeLimit (s)")
    ap.add_argument("--threads", type=int, default=None, help="Gurobi Threads")
    ap.add_argument("--keep-workspace", action="store_true",
                    help="keep <run_dir>/workspace (symlinks + the model's raw outputs)")
    ap.add_argument("--dry-run", action="store_true",
                    help="validate and print the command; solve nothing")
    args = ap.parse_args()

    config = dict(SMOKE_CONFIG, year=args.year, optimization_days=args.days)
    if args.time_limit is not None:
        config["time_limit"] = args.time_limit
    if args.threads is not None:
        config["threads"] = args.threads

    adapter = PathwaysAdapter()
    if adapter.data_root is None:
        return fail("PATHWAYS_DATA_ROOT is not set. See examples/pathways/README.md "
                    "for the Zenodo download and layout.")

    verdict = adapter.validate_config(config)
    if not verdict.ok:
        return fail("config rejected:\n  - " + "\n  - ".join(verdict.errors))

    run_dir = Path(args.run_dir)
    print(f"[smoke] adapter   : {adapter.name}")
    print(f"[smoke] model root: {adapter.model_root}")
    print(f"[smoke] data root : {adapter.data_root}")
    print(f"[smoke] python    : {adapter.python}")
    print(f"[smoke] config    : {config}")
    print(f"[smoke] run dir   : {run_dir}")
    if args.dry_run:
        print("[smoke] command   : " + " ".join(adapter.command(run_dir)))
        print("[smoke] dry run — nothing solved.")
        return 0

    print("[smoke] solving (minutes; Gurobi barrier, no crossover)…")
    record = run_and_record(adapter, config, run_dir)
    ex = record.execution
    print(f"[smoke] status={ex.termination_status} wall={ex.wall_seconds}s rc={ex.returncode}")

    log = run_dir / "solver.log"
    if not log.is_file() or not log.stat().st_size:
        return fail("no solver.log was captured")
    if "[pathways] stage: done" not in log.read_text(errors="replace"):
        print("[smoke] WARN: the driver never reached 'stage: done'")

    if ex.termination_status != "OPTIMAL":
        result = adapter.read_driver_result(run_dir) or {}
        return fail(f"expected OPTIMAL, got {ex.termination_status}: "
                    f"{result.get('reason', 'see solver.log')}")

    outputs = adapter.locate_outputs(run_dir)
    missing = [name for name in EXPECTED_OUTPUTS if name not in outputs]
    if missing:
        return fail(f"missing archived outputs: {missing} (have {sorted(outputs)})")

    # emissionValue.csv is headerless "name,value" (main.py:1410-1418): one
    # emission_target_mt row plus one row per emitting technology. Their sum is
    # exactly the left-hand side of the cap constraint at main.py:1337-1341.
    emissions = read_pairs(outputs["emissionValue"])
    print(f"[smoke] emissionValue.csv: {emissions}")
    components = [v for k, v in emissions.items()
                  if k.startswith("emission_") and k != "emission_target_mt"]
    if "emission_target_mt" not in emissions or not components:
        return fail(f"emissionValue.csv is not in the expected shape: {emissions}")
    try:
        realised = sum(float(v) for v in components)
        cap = float(emissions["emission_target_mt"])
    except ValueError:
        return fail(f"emissionValue.csv holds non-numeric values: {emissions}")
    if realised > cap + 1e-3:
        return fail(f"realised emissions {realised:.2f} Mt exceed the cap {cap:.2f} Mt — "
                    f"the emission constraint did not bind")
    print(f"[smoke] emissions {realised:.2f} Mt <= cap {cap:.2f} Mt  OK")

    objective = read_pairs(outputs["objValue"]).get("objValue")
    print(f"[smoke] objective: {objective} million RMB")

    if not args.keep_workspace:
        adapter.cleanup_workspace(run_dir)
        print("[smoke] workspace removed (outputs are archived in outputs/)")

    print("[smoke] PASS")
    return 0


def fail(message: str) -> int:
    print(f"[smoke] FAIL: {message}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
