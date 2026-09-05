"""The closed supervisory loop (Task 6) — model-agnostic.

Orchestrates run -> (diagnose/propose) -> refine -> run, keeping an iteration
ledger, refusing to repeat a config it has already tried, and stopping on a
satisfactory result, a budget, or a Tier-C policy block that needs a human.

The *reasoning* — turning a failed run into proposed changes — is supplied by a
``propose_fn`` callback. In live use the refiner skill (an LLM, e.g. via
``framework.agent_driver.make_propose_fn``) is that brain; in tests a
deterministic rule serves. Either way the guardrail is enforced here via
``apply_refinements``: a Tier-C proposal is never auto-applied, it stops the loop
for human sign-off. The loop cannot launder a policy relaxation.

**Ablation.** ``Supervisor(adapter, enforce_guardrail=False)`` is the unguarded
experimental condition: ``apply_refinements`` runs with ``enforce=False``, so a
legal Tier-C proposal is applied and the loop continues with the relaxed config
instead of returning ``needs_human``. That flag is the *only* behavioural
difference between the two conditions; dedup, stop criteria, and the audit
trail are identical.
"""

from __future__ import annotations

import inspect
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from framework.adapter import Adapter
from framework.interventions import InterventionSpec, ProposedChange
from framework.refine import apply_refinements
from framework.run_record import RunRecord
from framework.runner import run_and_record

# Given the latest run record, propose controlled changes (may be empty). A
# propose_fn may also accept ``run_dir=`` (keyword) to learn where that
# iteration's artifacts live; the supervisor passes it only when accepted.
ProposeFn = Callable[..., List[ProposedChange]]
# Called on a run that reached the target status: (record, run_dir) -> Any.
AnalyzeFn = Callable[[RunRecord, Path], Any]


@dataclass
class StopCriteria:
    target_status: str = "OPTIMAL"
    max_iters: int = 5


@dataclass
class SupervisorResult:
    outcome: str            # "solved" | "needs_human" | "exhausted" | "cycle" | "stuck"
    reason: str
    ledger: List[RunRecord] = field(default_factory=list)
    enforce_guardrail: bool = True

    @property
    def solved(self) -> bool:
        return self.outcome == "solved"

    @property
    def iterations(self) -> int:
        return len(self.ledger)


def _accepts_run_dir(fn: Callable[..., Any]) -> bool:
    """True if ``fn`` can take a ``run_dir`` keyword (explicitly or via **kwargs)."""
    try:
        params = inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return False
    if "run_dir" in params:
        return True
    return any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values())


class Supervisor:
    """Drives the iterate loop for one adapter."""

    def __init__(
        self,
        adapter: Adapter,
        spec: Optional[InterventionSpec] = None,
        enforce_guardrail: bool = True,
    ):
        self.adapter = adapter
        self.spec = spec or adapter.intervention_spec()
        self.enforce_guardrail = bool(enforce_guardrail)

    def _result(self, outcome: str, reason: str, ledger: List[RunRecord]) -> SupervisorResult:
        return SupervisorResult(outcome, reason, ledger, enforce_guardrail=self.enforce_guardrail)

    def run(
        self,
        config: Dict[str, Any],
        propose_fn: ProposeFn,
        run_root: str | Path,
        stop: Optional[StopCriteria] = None,
        analyze_fn: Optional[AnalyzeFn] = None,
    ) -> SupervisorResult:
        stop = stop or StopCriteria()
        run_root = Path(run_root)
        ledger: List[RunRecord] = []
        seen: set[str] = set()
        current = dict(config)
        parent: Optional[str] = None
        pass_run_dir = _accepts_run_dir(propose_fn)

        for i in range(stop.max_iters):
            h = RunRecord(config=current).config_hash
            if h in seen:
                return self._result("cycle", f"config {h} already tried; stopping to avoid a loop", ledger)
            seen.add(h)

            run_dir = run_root / f"iter{i:02d}_{h}"
            record = run_and_record(self.adapter, current, run_dir, parent_run=parent)
            ledger.append(record)
            parent = record.config_hash

            if record.execution.termination_status == stop.target_status:
                if analyze_fn is not None:
                    analyze_fn(record, run_dir)
                    record.save(run_dir / "run_record.json")
                return self._result("solved", f"reached {stop.target_status} at iteration {i}", ledger)

            proposals = propose_fn(record, run_dir=run_dir) if pass_run_dir else propose_fn(record)
            if not proposals:
                record.save(run_dir / "run_record.json")   # persist any diagnosis
                return self._result("stuck", f"no refinement proposed at iteration {i}", ledger)

            outcome = apply_refinements(record, proposals, self.spec, enforce=self.enforce_guardrail)
            record.save(run_dir / "run_record.json")

            if outcome.needs_human:
                return self._result(
                    "needs_human",
                    "a Tier-C policy change is the only proposed fix; stopping for human sign-off",
                    ledger,
                )
            if not outcome.applied:
                return self._result("stuck", "all proposals were rejected (illegal values)", ledger)

            current = outcome.next_config

        return self._result("exhausted", f"hit max_iters={stop.max_iters} without {stop.target_status}", ledger)
