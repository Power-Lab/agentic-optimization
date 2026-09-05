#!/usr/bin/env python3
"""Smoke test for the pypsa_toy adapter — the reproducibility anchor.

Runs the toy end to end through the real adapter (subprocess, streamed
``solver.log``, archived CSVs) and checks the answer against numbers this
machine can rederive from scratch. Because the model is seeded and the solver
is HiGHS, a passing run here means the whole pipeline — schema, network
builder, runner, status parsing, output archiving — is intact.

    python examples/pypsa_toy/smoke_test.py                # 1-day case, seconds
    python examples/pypsa_toy/smoke_test.py --days 14      # the full fixture
    python examples/pypsa_toy/smoke_test.py --record       # print the numbers
                                                           # as a JSON block to
                                                           # paste into EXPECTED

The first time you run this on a new machine there are no expected values to
compare against: run with ``--record``, sanity-check the numbers by eye against
the bounds printed alongside them, and paste the block into ``EXPECTED`` below.
Thereafter the test is a strict regression check (default tolerance 1 %).

Requires an interpreter with pypsa, linopy and highspy — set ``PYPSA_PYTHON``
if that is not ``~/miniforge3/bin/python``.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(ROOT))

from adapters.pypsa_toy import PypsaToyAdapter  # noqa: E402

RUN_ROOT = ROOT / "runs"
TOLERANCE = 0.01

#: Headline numbers per horizon length: {days: {metric: value}}. Empty until
#: someone runs --record; an empty entry turns the comparison into a report.
EXPECTED: dict[str, dict[str, float]] = {}

#: Bounds that must hold whatever the exact numbers are. These encode the
#: model's physics, not one machine's arithmetic, so they are always checked.
def invariants(cost: dict, days: int) -> list[str]:
    """Return a list of violated invariants (empty when all hold)."""
    from adapters.pypsa_toy.network import horizon_summary

    facts = horizon_summary({"snapshots_days": days})
    bad = []
    if cost["shed_share_of_demand"] > 1e-9:
        bad.append(f"shed_share_of_demand = {cost['shed_share_of_demand']} but "
                   "allow_load_shedding is false — no demand may go unserved")
    if not 0.0 <= cost["re_share_of_generation"] <= 0.95:
        bad.append(f"re_share_of_generation = {cost['re_share_of_generation']} is outside 0..0.95")
    if cost["emissions_t"] < facts["emissions_floor_t"] - 1.0:
        bad.append(f"emissions_t = {cost['emissions_t']} is below the must-run coal floor "
                   f"of {facts['emissions_floor_t']} t — the network is wrong")
    if cost["emissions_t"] > facts["emissions_all_coal_ceiling_t"] + 1.0:
        bad.append(f"emissions_t = {cost['emissions_t']} exceeds the all-coal ceiling "
                   f"of {facts['emissions_all_coal_ceiling_t']} t")
    if cost["total_system_cost_usd"] <= 0:
        bad.append("total_system_cost_usd is not positive")
    if abs(cost["demand_mwh"] - facts["demand_mwh"]) > 0.01 * facts["demand_mwh"]:
        bad.append(f"demand_mwh = {cost['demand_mwh']} does not match the profile's "
                   f"{facts['demand_mwh']}")
    if int(cost["snapshots"]) != days * 24:
        bad.append(f"snapshots = {cost['snapshots']}, expected {days * 24}")
    return bad


HEADLINES = [
    "objective_usd",
    "total_system_cost_usd",
    "emissions_t",
    "re_share_of_generation",
    "gas_energy_share",
    "average_cost_usd_per_mwh",
]


def read_cost_results(path: Path) -> dict:
    with path.open() as fh:
        row = next(iter(csv.DictReader(fh)))
    out = {}
    for key, value in row.items():
        try:
            out[key] = float(value)
        except (TypeError, ValueError):
            out[key] = value
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--days", type=int, default=1, help="snapshots_days (1..14)")
    ap.add_argument("--record", action="store_true",
                    help="print the headline numbers instead of comparing them")
    ap.add_argument("--tolerance", type=float, default=TOLERANCE)
    ap.add_argument("--run-dir", default=None)
    args = ap.parse_args(argv)

    adapter = PypsaToyAdapter()
    config = {
        "snapshots_days": args.days,
        "allow_load_shedding": False,
        "line_expansion_allowed": True,
        "demand_scale": 1.0,
        "solver": "highs",
        "time_limit": 600.0,
        "threads": 1,
    }

    verdict = adapter.validate_config(config)
    if not verdict.ok:
        print("[smoke] config is invalid:")
        for err in verdict.errors:
            print(f"   - {err}")
        return 2

    probe = adapter.check_interpreter()
    if probe.returncode != 0:
        print(f"[smoke] {adapter.python} cannot import pypsa/linopy/highspy:")
        print(probe.stderr.strip()[-800:])
        print("[smoke] set PYPSA_PYTHON to an interpreter that has them.")
        return 2
    print(f"[smoke] interpreter {adapter.python} -> pypsa {probe.stdout.strip()}")

    run_dir = Path(args.run_dir) if args.run_dir else RUN_ROOT / f"smoke_pypsa_toy_{args.days}d"
    print(f"[smoke] run dir = {run_dir}")
    start = time.monotonic()
    execution = adapter.run(config, run_dir, on_line=lambda line: None)
    print(f"[smoke] status = {execution.termination_status} "
          f"({execution.wall_seconds:.1f} s wall, exit {execution.returncode})")

    if execution.termination_status != "OPTIMAL":
        print(f"[smoke] FAIL: expected OPTIMAL, got {execution.termination_status} "
              f"(origin {execution.error_origin}). See {run_dir / 'solver.log'}.")
        return 1

    outputs = adapter.locate_outputs(run_dir)
    missing = [name for name in ("generator_results", "storage_results", "line_results",
                                 "cost_results", "emissions_results", "nse_results")
               if name not in outputs]
    if missing:
        print(f"[smoke] FAIL: missing output files {missing} (got {sorted(outputs)})")
        return 1
    print(f"[smoke] outputs: {', '.join(sorted(outputs))}")

    log = (run_dir / "solver.log").read_text()
    for marker in ("[pypsa_toy] horizon emissions_floor_t",
                   "[pypsa_toy] network built",
                   "[pypsa_toy] status: OPTIMAL"):
        if marker not in log:
            print(f"[smoke] FAIL: solver.log is missing the marker {marker!r}")
            return 1

    cost = read_cost_results(outputs["cost_results"])
    violations = invariants(cost, args.days)
    if violations:
        print("[smoke] FAIL: physical invariants violated:")
        for v in violations:
            print(f"   - {v}")
        return 1
    print("[smoke] physical invariants hold")

    headlines = {k: cost[k] for k in HEADLINES if k in cost}
    if args.record or str(args.days) not in EXPECTED:
        print("[smoke] headline numbers (paste into EXPECTED to lock them in):")
        print(json.dumps({str(args.days): headlines}, indent=2))
        if not args.record:
            print("[smoke] PASS (no recorded expectations for this horizon yet)")
        return 0

    expected = EXPECTED[str(args.days)]
    failures = []
    for key, want in expected.items():
        got = headlines.get(key)
        if got is None:
            failures.append(f"{key}: missing from cost_results")
        elif want == 0 and abs(got) > 1e-9:
            failures.append(f"{key}: expected 0, got {got}")
        elif want != 0 and abs(got - want) / abs(want) > args.tolerance:
            failures.append(f"{key}: expected {want}, got {got} "
                            f"({100 * (got - want) / want:+.2f} %)")
    if failures:
        print(f"[smoke] FAIL: {len(failures)} headline(s) outside ±{100 * args.tolerance:g} %:")
        for f in failures:
            print(f"   - {f}")
        return 1

    print(f"[smoke] all {len(expected)} headlines within ±{100 * args.tolerance:g} % "
          f"({time.monotonic() - start:.1f} s total)")
    print("[smoke] PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
