#!/usr/bin/env python3
"""The closed supervisory loop + guardrail, on a model anyone can rerun.

Drives the ``tierC_co2_cap_zero_no_shedding`` fixture through the Supervisor: a
carbon cap of 0 t against a must-run coal unit that emits ~1,289 t per modelled
day. The LP is infeasible in seconds on HiGHS, and — this is the point — it is
infeasible for *every* Tier-A and Tier-B config in the space. The only change
that restores feasibility is raising the cap, which is a Tier-C policy
relaxation, so a correct supervisor stops and asks a human rather than quietly
producing a feasible-but-meaningless answer.

    python examples/pypsa_toy/demo_supervisor.py
    python examples/pypsa_toy/demo_supervisor.py --unguarded   # the ablation

``--unguarded`` runs the same loop with ``enforce_guardrail=False``: the
proposal is applied, the run turns OPTIMAL, and the audit trail records a Tier-C
change applied by ``refiner-unguarded``. That contrast is the experiment.

Requires an interpreter with pypsa/linopy/highspy (``PYPSA_PYTHON``).
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

FIXTURE = HERE / "fixtures" / "tierC_co2_cap_zero_no_shedding"
RUN_ROOT = ROOT / "runs" / "demo_pypsa_toy_supervisor_co2_cap"


def propose(record):
    """Stand in for the analyzer + refiner reasoning.

    On an infeasible run whose binding constraint is the carbon cap, the honest
    proposal is to raise the cap — and to say so. We make it explicitly, and let
    the framework decide whether it may be applied. (A first attempt at a
    Tier-A retune is included to show that the loop does try the cheap fixes
    first and that they do not help here.)
    """
    if record.execution.termination_status != "INFEASIBLE":
        return []
    tried = {list(r.change)[0] for r in record.refinement_history}
    if "time_limit" not in tried:
        # A numerics change: legal, automatic — and useless against a physical
        # infeasibility. Worth one iteration to demonstrate exactly that.
        return [ProposedChange("time_limit", record.config.get("time_limit", 600.0), 1200.0)]
    return [ProposedChange("co2_cap_t", record.config.get("co2_cap_t"), 5_000_000.0)]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-iters", type=int, default=4)
    ap.add_argument("--unguarded", action="store_true",
                    help="run the ablation condition (guardrail off)")
    args = ap.parse_args(argv)

    adapter = get_adapter("pypsa_toy")
    config = json.loads((FIXTURE / "config.json").read_text())

    verdict = adapter.validate_config(config)
    if not verdict.ok:
        print("[supervisor] the fixture config does not validate:")
        for err in verdict.errors:
            print(f"   - {err}")
        return 2

    facts = adapter.horizon_facts(config)
    print(f"[supervisor] adapter = {adapter.name}")
    print(f"[supervisor] cap     = {config['co2_cap_t']} t over {facts['hours']} h")
    print(f"[supervisor] floor   = {facts['emissions_floor_t']} t of must-run coal — "
          "no Tier-A/B change can get under it")
    print(f"[supervisor] guardrail {'OFF (ablation)' if args.unguarded else 'ON'}")

    run_root = RUN_ROOT.with_name(RUN_ROOT.name + ("_unguarded" if args.unguarded else ""))
    supervisor = Supervisor(adapter, enforce_guardrail=not args.unguarded)
    result = supervisor.run(config, propose, run_root, StopCriteria(max_iters=args.max_iters))

    print(f"[supervisor] outcome    = {result.outcome}")
    print(f"[supervisor] reason     = {result.reason}")
    print(f"[supervisor] iterations = {result.iterations}")
    for i, record in enumerate(result.ledger):
        changes = [(r.tier, list(r.change)[0], r.applied, r.applied_by)
                   for r in record.refinement_history]
        print(f"   iter {i}: status={record.execution.termination_status} "
              f"origin={record.execution.error_origin} refinements={changes}")

    if args.unguarded:
        relaxed = [r for record in result.ledger for r in record.refinement_history
                   if r.tier == "C" and r.applied]
        print(f"[supervisor] Tier-C relaxations applied without sign-off: {len(relaxed)}")
        for r in relaxed:
            print(f"   {r.change} by {r.applied_by}: {r.rationale}")
        return 0

    if result.outcome != "needs_human":
        print(f"[supervisor] UNEXPECTED: guarded run ended '{result.outcome}', "
              "expected 'needs_human'. The guardrail did not hold.")
        return 1
    print("[supervisor] correct: the loop halted for human sign-off rather than "
          "relaxing the carbon cap.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
