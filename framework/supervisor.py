"""The closed supervisory loop (Task 6) — model-agnostic.

Orchestrates run -> (diagnose/propose) -> refine -> run, keeping an iteration
ledger, refusing to repeat a config it has already tried, and stopping on a
satisfactory result, a budget, or a Tier-C policy block that needs a human.

The *reasoning* — turning a failed run into proposed changes — is supplied by a
``propose_fn`` callback. In live use the refiner skill (an LLM) is that brain; in
tests a deterministic rule serves. Either way the guardrail is enforced here via
``apply_refinements``: a Tier-C proposal is never auto-applied, it stops the loop
for human sign-off. The loop cannot launder a policy relaxation.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from framework.adapter import Adapter
from framework.interventions import InterventionSpec, ProposedChange
from framework.refine import apply_refinements
from framework.run_record import RunRecord
from framework.runner import run_and_record

# Given the latest run record, propose controlled changes (may be empty).
ProposeFn = Callable[[RunRecord], List[ProposedChange]]


@dataclass
class StopCriteria:
    target_status: str = "OPTIMAL"
    max_iters: int = 5


@dataclass
class SupervisorResult:
    outcome: str            # "solved" | "needs_human" | "exhausted" | "cycle" | "stuck"
    reason: str
    ledger: List[RunRecord] = field(default_factory=list)

    @property
    def solved(self) -> bool:
        return self.outcome == "solved"

    @property
    def iterations(self) -> int:
        return len(self.ledger)


class Supervisor:
    """Drives the iterate loop for one adapter."""

    def __init__(self, adapter: Adapter, spec: Optional[InterventionSpec] = None):
        self.adapter = adapter
        self.spec = spec or adapter.intervention_spec()

    def run(
        self,
        config: Dict[str, Any],
        propose_fn: ProposeFn,
        run_root: str | Path,
        stop: Optional[StopCriteria] = None,
    ) -> SupervisorResult:
        stop = stop or StopCriteria()
        run_root = Path(run_root)
        ledger: List[RunRecord] = []
        seen: set[str] = set()
        current = dict(config)
        parent: Optional[str] = None

        for i in range(stop.max_iters):
            h = RunRecord(config=current).config_hash
            if h in seen:
                return SupervisorResult("cycle", f"config {h} already tried; stopping to avoid a loop", ledger)
            seen.add(h)

            run_dir = run_root / f"iter{i:02d}_{h}"
            record = run_and_record(self.adapter, current, run_dir, parent_run=parent)
            ledger.append(record)
            parent = record.config_hash

            if record.execution.termination_status == stop.target_status:
                return SupervisorResult("solved", f"reached {stop.target_status} at iteration {i}", ledger)

            proposals = propose_fn(record)
            if not proposals:
                return SupervisorResult("stuck", f"no refinement proposed at iteration {i}", ledger)

            outcome = apply_refinements(record, proposals, self.spec)
            record.save(run_dir / "run_record.json")

            if outcome.needs_human:
                return SupervisorResult(
                    "needs_human",
                    "a Tier-C policy change is the only proposed fix; stopping for human sign-off",
                    ledger,
                )
            if not outcome.applied:
                return SupervisorResult("stuck", "all proposals were rejected (illegal values)", ledger)

            current = outcome.next_config

        return SupervisorResult("exhausted", f"hit max_iters={stop.max_iters} without {stop.target_status}", ledger)
