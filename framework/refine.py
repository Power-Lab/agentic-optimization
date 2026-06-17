"""Refinement enforcement — the code that makes the guardrail non-negotiable.

The refiner *skill* (an LLM) proposes changes; this module *enforces* the
tiered taxonomy deterministically. The LLM never decides whether a change is
safe to auto-apply — :func:`apply_refinements` does, via
:func:`framework.interventions.decide`. Tier A/B are applied to a fresh config;
Tier C (and rejected values) are recorded but NOT applied, and surfaced for a
human. Every outcome is appended to the run record's audit trail.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Any, Dict, List

from framework.interventions import InterventionSpec, ProposedChange, decide
from framework.run_record import Refinement, RunRecord


@dataclass
class RefineOutcome:
    """Result of applying a batch of proposed changes to a config."""

    next_config: Dict[str, Any]          # config with Tier A/B changes applied
    applied: List[Refinement]            # changes written into next_config
    blocked: List[Refinement]            # Tier C, withheld for human sign-off
    rejected: List[Refinement]           # illegal values, never applied

    @property
    def needs_human(self) -> bool:
        return bool(self.blocked)


def apply_refinements(
    record: RunRecord,
    proposals: List[ProposedChange],
    spec: InterventionSpec,
) -> RefineOutcome:
    """Classify and (where permitted) apply a batch of proposed changes.

    Returns a new config rather than mutating ``record.config``; the caller
    runs it as a child run (``parent_run = record.config_hash``). The record's
    ``refinement_history`` is appended to for the full audit trail.
    """
    next_config = copy.deepcopy(record.config)
    applied: List[Refinement] = []
    blocked: List[Refinement] = []
    rejected: List[Refinement] = []

    for p in proposals:
        # Ensure 'before' reflects the actual current value.
        before = next_config.get(p.key, p.before)
        change = ProposedChange(key=p.key, before=before, after=p.after)
        decision = decide(change, spec)

        entry = Refinement(
            tier=decision.tier.value,
            change={p.key: [before, p.after]},
            rationale=decision.reason,
            applied=False,
        )

        if decision.rejected:
            rejected.append(entry)
        elif decision.auto_apply:
            next_config[p.key] = p.after
            entry.applied = True
            applied.append(entry)
        else:
            # Tier C — legal but policy-grade. Never silent.
            blocked.append(entry)

        record.refinement_history.append(entry)

    return RefineOutcome(
        next_config=next_config,
        applied=applied,
        blocked=blocked,
        rejected=rejected,
    )
