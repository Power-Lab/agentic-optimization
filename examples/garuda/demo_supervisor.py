#!/usr/bin/env python3
"""Live demonstration of the closed supervisory loop + guardrail on garuda.

Points the Supervisor at the ``tierC_co2_floor`` fixture: a clean run on maluku
with CO2_limit = -1 using the **dispatch** engine, so the solver reports
INFEASIBLE within seconds on HiGHS (no licence). The only fix is relaxing the
carbon cap (Tier C), so a correct supervisor must NOT loop forever or launder the
relaxation — it halts with ``needs_human`` after one iteration. Contrast with
tests/test_supervisor.py, which shows a Tier-A case converging to OPTIMAL.

    python examples/garuda/demo_supervisor.py [--max-iters 5]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(ROOT))

from framework import ProposedChange, StopCriteria, Supervisor, get_adapter  # noqa: E402

FIXTURE = HERE / "fixtures" / "tierC_co2_floor"
RUN_ROOT = ROOT / "runs" / "demo_garuda_supervisor_co2_floor"


def propose(record):
    """Stand in for the analyzer+refiner reasoning. On an infeasible run whose
    binding constraint is the CO2 cap, the only feasibility fix is to relax the
    cap — a Tier-C policy change. We propose it honestly; the supervisor will
    refuse to auto-apply it and stop for a human."""
    if record.execution.termination_status == "INFEASIBLE":
        return [ProposedChange("CO2_limit", record.config.get("CO2_limit"), 1e12)]
    return []


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-iters", type=int, default=5)
    args = ap.parse_args()

    adapter = get_adapter("garuda")
    config = json.loads((FIXTURE / "config.json").read_text())

    print(f"[supervisor] adapter = {adapter.name}")
    print("[supervisor] driving the infeasible CO2-cap scenario through the loop…")
    result = Supervisor(adapter).run(config, propose, RUN_ROOT,
                                     StopCriteria(max_iters=args.max_iters))

    print(f"[supervisor] outcome    = {result.outcome}")
    print(f"[supervisor] reason     = {result.reason}")
    print(f"[supervisor] iterations = {result.iterations}")
    for i, rec in enumerate(result.ledger):
        print(f"   iter {i}: status={rec.execution.termination_status} "
              f"origin={rec.execution.error_origin} "
              f"refinements={[(r.tier, r.applied) for r in rec.refinement_history]}")

    ok = result.outcome == "needs_human" and result.iterations == 1
    if ok:
        print("[supervisor] loop halted for human sign-off — did not loop, "
              "did not relax the cap")
    else:
        print(f"[supervisor] unexpected outcome: {result.outcome}")
        log = RUN_ROOT.glob("iter00_*/solver.log")
        for p in log:
            print(p.read_text()[-1500:])
    print("[supervisor]", "DEMO PASSED" if ok else "DEMO FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
