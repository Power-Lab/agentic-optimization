#!/usr/bin/env python3
"""End-to-end demonstration of the guardrailed refine loop on the garuda model.

Runs the ``tierC_co2_floor`` fixture live (maluku dispatch, clean run with
CO2_limit = -1 → INFEASIBLE in seconds on HiGHS), then shows the refiner
refusing to "fix" it by relaxing the carbon cap — the scientific-integrity
guardrail in action. Contrasts with a Tier-A change that *is* auto-applied and a
Tier-B change that is applied but flagged.

    python examples/garuda/demo_refine_loop.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(ROOT))

from adapters.garuda import GarudaAdapter                # noqa: E402
from framework import (                                  # noqa: E402
    Diagnosis, ProposedChange, apply_refinements, run_and_record,
)

FIXTURE = HERE / "fixtures" / "tierC_co2_floor"
RUN_DIR = ROOT / "runs" / "demo_garuda_co2_floor"
RELAXED_CAP = 1e12


def main() -> int:
    adapter = GarudaAdapter()
    spec = adapter.intervention_spec()
    config = json.loads((FIXTURE / "config.json").read_text())
    expected = json.loads((FIXTURE / "expected.json").read_text())

    print("=" * 70)
    print("STEP 1 — model-runner: execute the (infeasible) config live")
    print("=" * 70)
    record = run_and_record(adapter, config, RUN_DIR, preflight=True)
    status = record.execution.termination_status
    print(f"  termination_status = {status}  origin = {record.execution.error_origin}  "
          f"({record.execution.wall_seconds}s, rc={record.execution.returncode})")
    if status != expected["expected_status"]:
        print(f"  ! expected {expected['expected_status']}, got {status}; aborting demo")
        print((RUN_DIR / "solver.log").read_text()[-1500:])
        return 1

    print("\n" + "=" * 70)
    print("STEP 2 — log-analyzer: diagnose root cause")
    print("=" * 70)
    record.log_diagnosis = Diagnosis(
        status="INFEASIBLE",
        root_cause="Grid CO2 cap (CO2_limit = -1) is below the physical floor: "
                   "emissions are non-negative, so the cap is unsatisfiable. RE_limit=0 "
                   "makes the renewable floor inert, so the cap is the only binding policy.",
        evidence=["log: Dispatch is infeasible (...)"],
        suggested_intervention_tier="C",
        confidence=0.95,
    )
    record.save(RUN_DIR / "run_record.json")
    print(f"  root cause: {record.log_diagnosis.root_cause}")
    print(f"  suggested tier: {record.log_diagnosis.suggested_intervention_tier}")

    print("\n" + "=" * 70)
    print("STEP 3 — refiner: the only feasibility fix relaxes the CO2 cap")
    print("=" * 70)
    proposal = ProposedChange("CO2_limit", config["CO2_limit"], RELAXED_CAP)
    outcome = apply_refinements(record, [proposal], spec)
    record.save(RUN_DIR / "run_record.json")

    blocked_ok = (
        outcome.needs_human
        and outcome.next_config["CO2_limit"] == config["CO2_limit"]  # unchanged
        and not outcome.applied
    )
    print(f"  proposed: relax CO2_limit {config['CO2_limit']} -> {RELAXED_CAP:g}")
    print(f"  framework ruling: {outcome.blocked[0].rationale if outcome.blocked else 'NONE'}")
    print(f"  config CO2_limit after refiner: {outcome.next_config['CO2_limit']}  (unchanged)")
    print(f"  needs_human: {outcome.needs_human}")
    if blocked_ok:
        print("  GUARDRAIL HELD: the cap was NOT silently relaxed.")
    else:
        print("  GUARDRAIL FAILED: a policy constraint was auto-applied.")

    print("\n  -> Human sign-off required. Trade-off to present:")
    print("    The 2030 grid carbon cap as specified is physically unattainable.")
    print("    Restoring feasibility means either (a) more buildable clean capacity /")
    print("    different inputs, or (b) loosening the cap or dropping the clean run —")
    print("    which changes the study's claim. The agent will not choose (b) alone.")

    print("\n" + "=" * 70)
    print("CONTRAST — a Tier-A numeric change IS auto-applied")
    print("=" * 70)
    a_outcome = apply_refinements(record, [ProposedChange("time_limit", None, 3600.0)], spec)
    applied_ok = a_outcome.next_config.get("time_limit") == 3600.0 and not a_outcome.needs_human
    print("  proposed: set time_limit -> 3600 s")
    print(f"  applied: {bool(a_outcome.applied)}  needs_human: {a_outcome.needs_human}")
    print("  auto-applied (search setting, not problem meaning)" if applied_ok
          else "  unexpected")

    print("\n" + "=" * 70)
    print("CONTRAST — a Tier-B sanctioned parameter is applied AND flagged")
    print("=" * 70)
    b_outcome = apply_refinements(record, [ProposedChange("engine", "dispatch", "expansion")], spec)
    b_ok = b_outcome.next_config.get("engine") == "expansion" and not b_outcome.needs_human
    print("  proposed: engine dispatch -> expansion")
    print(f"  applied: {bool(b_outcome.applied)}  tier: "
          f"{b_outcome.applied[0].tier if b_outcome.applied else '-'}  (surfaced in the audit trail)")

    print("\n" + "=" * 70)
    ok = blocked_ok and applied_ok and b_ok
    print("RESULT:", "DEMO PASSED" if ok else "DEMO FAILED")
    print("Audit trail:", RUN_DIR / "run_record.json")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
