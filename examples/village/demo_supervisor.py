#!/usr/bin/env python3
"""Live demonstration of the closed supervisory loop (Task 6) + guardrail.

Points the Supervisor at the infeasible CO2-cap fixture. The only fix is relaxing
the carbon cap (Tier C), so a correct supervisor must NOT loop forever or launder
the relaxation — it halts with `needs_human` after one iteration. Contrast with
test_supervisor.py, which shows a Tier-A case converging to OPTIMAL.

    python examples/village/demo_supervisor.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(ROOT))

from framework import ProposedChange, StopCriteria, Supervisor, get_adapter  # noqa: E402

FIXTURE = HERE / "fixtures" / "infeasible_negative_co2"
RUN_ROOT = ROOT / "runs" / "demo_supervisor_infeasible"


def propose(record):
    """Stand in for the analyzer+refiner reasoning. On an infeasible run whose
    binding constraint is the CO2 cap, the only feasibility fix is to relax the
    cap — a Tier-C policy change. We propose it honestly; the supervisor will
    refuse to auto-apply it and stop for a human."""
    if record.execution.termination_status == "INFEASIBLE":
        return [ProposedChange("CO2_limit", record.config.get("CO2_limit"), 5_820_000)]
    return []


def main() -> int:
    adapter = get_adapter()
    config = json.loads((FIXTURE / "config.json").read_text())

    print(f"[supervisor] adapter = {adapter.name}")
    print("[supervisor] driving the infeasible CO2-cap scenario through the loop…")
    result = Supervisor(adapter).run(config, propose, RUN_ROOT, StopCriteria(max_iters=5))

    print(f"[supervisor] outcome   = {result.outcome}")
    print(f"[supervisor] iterations = {result.iterations}")
    for i, rec in enumerate(result.ledger):
        print(f"   iter {i}: status={rec.execution.termination_status} "
              f"refinements={[r.tier for r in rec.refinement_history]}")

    ok = result.outcome == "needs_human" and result.iterations == 1
    if ok:
        print("[supervisor] ✅ loop halted for human sign-off — did not loop, "
              "did not relax the cap")
    else:
        print(f"[supervisor] ❌ unexpected outcome: {result.outcome}")
    print("[supervisor]", "✅ DEMO PASSED" if ok else "❌ DEMO FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
