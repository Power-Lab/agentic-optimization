#!/usr/bin/env python3
"""One guardrailed refine step on the toy model — both halves of the rule.

The interesting question about a guardrail is not "does it block things" but
"does it block the right things". This demo runs one genuinely infeasible
config and shows the framework separating two proposals that look identical
from the outside — both restore feasibility, both are one JSON key — into

  * ``line_expansion_allowed = true`` — Tier B, applied automatically and
    flagged, because the modeller sanctioned it as a parameter of the study; and
  * ``allow_load_shedding = true`` — Tier C, withheld, because "some customers
    go dark" is a different study, not a parameter of this one.

The fixture is ``tierB_demand_scale``: demand x10 with the transmission lines
frozen at their existing ratings, so the east bus cannot be served. No policy
constraint is set at all — this is the case where over-blocking would be the
error, and the loop must reach a solved run on its own.

    python examples/pypsa_toy/demo_refine_loop.py

Requires an interpreter with pypsa/linopy/highspy (``PYPSA_PYTHON``).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(ROOT))

from adapters.pypsa_toy import PypsaToyAdapter          # noqa: E402
from framework import (                                 # noqa: E402
    Diagnosis, ProposedChange, apply_refinements, run_and_record,
)

FIXTURE = HERE / "fixtures" / "tierB_demand_scale"
RUN_DIR = ROOT / "runs" / "demo_pypsa_toy_refine"
RETRY_DIR = ROOT / "runs" / "demo_pypsa_toy_refine_retry"


def banner(step: str, title: str) -> None:
    print("=" * 72)
    print(f"STEP {step} — {title}")
    print("=" * 72)


def main() -> int:
    adapter = PypsaToyAdapter()
    spec = adapter.intervention_spec()
    config = json.loads((FIXTURE / "config.json").read_text())
    expected = json.loads((FIXTURE / "expected.json").read_text())

    banner("1", "model-runner: execute the config live")
    record = run_and_record(adapter, config, RUN_DIR, preflight=True)
    status = record.execution.termination_status
    print(f"  termination_status = {status}  origin = {record.execution.error_origin}  "
          f"({record.execution.wall_seconds}s, rc={record.execution.returncode})")
    if status != expected["expected_status"]:
        print(f"  ! expected {expected['expected_status']}, got {status}; aborting demo")
        log = RUN_DIR / "solver.log"
        if log.exists():
            print(log.read_text()[-1500:])
        return 1

    banner("2", "log-analyzer: why is it infeasible?")
    facts = adapter.horizon_facts(config)
    print(f"  peak demand          = {facts['peak_demand_mw']:.0f} MW")
    print("  east import ceiling  = 600 MW (north-east 400 + east-south 200, lines frozen)")
    print("  east local build     = 600 MW wind + 500 MW battery")
    print("  -> the east bus cannot be served at peak. Nothing about the carbon")
    print("     cap or the renewable floor is involved: both are null here.")
    record.log_diagnosis = Diagnosis(
        status=status,
        root_cause="transmission capacity shortfall at the east bus with line "
                   "expansion disabled and demand scaled x10",
        evidence=["solver.log: [pypsa_toy] status: INFEASIBLE",
                  f"solver.log: [pypsa_toy] horizon peak_demand_mw = {facts['peak_demand_mw']}"],
        suggested_intervention_tier="B",
        confidence=0.9,
    )

    banner("3", "refiner: two proposals, one rule")
    proposals = [
        ProposedChange("line_expansion_allowed", config["line_expansion_allowed"], True),
        ProposedChange("allow_load_shedding", config["allow_load_shedding"], True),
    ]
    outcome = apply_refinements(record, proposals, spec)
    for refinement in record.refinement_history:
        verdict = "APPLIED" if refinement.applied else "BLOCKED (needs human sign-off)"
        print(f"  tier {refinement.tier}  {refinement.change}  -> {verdict}")
        print(f"        {refinement.rationale}")
    print(f"  needs_human = {outcome.needs_human}")

    if outcome.next_config.get("allow_load_shedding") is True:
        print("  ! the policy key was applied — the guardrail did not hold")
        return 1

    banner("4", "model-runner: re-run with the sanctioned change only")
    retry = run_and_record(adapter, outcome.next_config, RETRY_DIR,
                           parent_run=record.config_hash, preflight=True)
    print(f"  termination_status = {retry.execution.termination_status} "
          f"({retry.execution.wall_seconds}s)")
    if retry.execution.termination_status != "OPTIMAL":
        print("  ! the Tier-B fix did not restore feasibility; see "
              f"{RETRY_DIR / 'solver.log'}")
        return 1

    outputs = adapter.locate_outputs(RETRY_DIR)
    if "line_results" in outputs:
        print(f"  built transmission (line_results.csv):")
        for line in outputs["line_results"].read_text().splitlines()[:5]:
            print(f"    {line}")

    print()
    print("The loop solved the problem without touching a policy key. That is the")
    print("half of the guardrail that is easy to forget: it has to let the honest")
    print("fix through, or it is just an obstacle people learn to route around.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
