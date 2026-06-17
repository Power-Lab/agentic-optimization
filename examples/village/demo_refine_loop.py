#!/usr/bin/env python3
"""End-to-end demonstration of the guardrailed refine loop (scope B).

Runs the `infeasible_negative_co2` fixture live, then shows the refiner refusing
to "fix" it by relaxing the carbon cap — the scientific-integrity guardrail in
action. Contrasts with a Tier-A change that *is* auto-applied.

    python examples/village/demo_refine_loop.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(ROOT))

from adapters.village import VillageAdapter            # noqa: E402
from framework import (                                # noqa: E402
    Diagnosis, ProposedChange, RunRecord, apply_refinements, run_and_record,
)

FIXTURE = HERE / "fixtures" / "infeasible_negative_co2"
RUN_DIR = ROOT / "runs" / "demo_infeasible_co2"


def main() -> int:
    adapter = VillageAdapter()
    spec = adapter.intervention_spec()
    config = json.loads((FIXTURE / "config.json").read_text())

    print("=" * 70)
    print("STEP 1 — model-runner: execute the (infeasible) config live")
    print("=" * 70)
    record = run_and_record(adapter, config, RUN_DIR, preflight=True)
    status = record.execution.termination_status
    print(f"  termination_status = {status}  ({record.execution.wall_seconds}s)")
    if status != "INFEASIBLE":
        print(f"  ! expected INFEASIBLE, got {status}; aborting demo")
        return 1

    print("\n" + "=" * 70)
    print("STEP 2 — log-analyzer: diagnose root cause")
    print("=" * 70)
    record.log_diagnosis = Diagnosis(
        status="INFEASIBLE",
        root_cause="Grid CO2 cap (CO2_limit = -1) is below the physical floor: "
                   "emissions are non-negative, so the cap is unsatisfiable.",
        evidence=["log: The model is infeasible."],
        suggested_intervention_tier="C",
        confidence=0.95,
    )
    record.save(RUN_DIR / "run_record.json")
    print(f"  root cause: {record.log_diagnosis.root_cause}")
    print(f"  suggested tier: {record.log_diagnosis.suggested_intervention_tier}")

    print("\n" + "=" * 70)
    print("STEP 3 — refiner: the only feasibility fix relaxes the CO2 cap")
    print("=" * 70)
    proposal = ProposedChange("CO2_limit", config["CO2_limit"], 5_820_000)
    outcome = apply_refinements(record, [proposal], spec)
    record.save(RUN_DIR / "run_record.json")

    blocked_ok = (
        outcome.needs_human
        and outcome.next_config["CO2_limit"] == config["CO2_limit"]  # unchanged
        and not outcome.applied
    )
    print(f"  proposed: relax CO2_limit {config['CO2_limit']} -> 5,820,000")
    print(f"  framework ruling: {outcome.blocked[0].rationale if outcome.blocked else 'NONE'}")
    print(f"  config CO2_limit after refiner: {outcome.next_config['CO2_limit']}  (unchanged)")
    print(f"  needs_human: {outcome.needs_human}")
    if blocked_ok:
        print("  ✅ GUARDRAIL HELD: the cap was NOT silently relaxed.")
    else:
        print("  ❌ GUARDRAIL FAILED: a policy constraint was auto-applied.")

    print("\n  → Human sign-off required. Trade-off to present:")
    print("    The 2030 grid carbon cap as specified is physically unattainable.")
    print("    Restoring feasibility means either (a) allowing more buildable")
    print("    clean capacity / inputs, or (b) loosening the cap — which changes")
    print("    the study's claim. The agent will not choose (b) on its own.")

    print("\n" + "=" * 70)
    print("CONTRAST — a Tier-A numeric change IS auto-applied")
    print("=" * 70)
    a_outcome = apply_refinements(record, [ProposedChange("mipgap", None, 0.05)], spec)
    applied_ok = a_outcome.next_config.get("mipgap") == 0.05 and not a_outcome.needs_human
    print(f"  proposed: set mipgap -> 0.05")
    print(f"  applied: {bool(a_outcome.applied)}  needs_human: {a_outcome.needs_human}")
    print("  ✅ auto-applied (search setting, not problem meaning)" if applied_ok
          else "  ❌ unexpected")

    print("\n" + "=" * 70)
    ok = blocked_ok and applied_ok
    print("RESULT:", "✅ DEMO PASSED" if ok else "❌ DEMO FAILED")
    print("Audit trail:", RUN_DIR / "run_record.json")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
