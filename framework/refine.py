"""Refinement enforcement — the code that makes the guardrail non-negotiable.

The refiner *skill* (an LLM) proposes changes; this module *enforces* the
tiered taxonomy deterministically. The LLM never decides whether a change is
safe to auto-apply — :func:`apply_refinements` does, via
:func:`framework.interventions.decide`. Tier A/B are applied to a fresh config;
Tier C (and rejected values) are recorded but NOT applied, and surfaced for a
human. Every outcome is appended to the run record's audit trail.

**Ablation switch.** ``apply_refinements(..., enforce=False)`` is the
*unguarded* condition of the experimental protocol: every *legal* proposal is
applied regardless of tier. The audit entry still records the true tier, so the
harm is measurable afterwards (``tier == "C"`` and ``applied`` with
``applied_by == "refiner-unguarded"``). Illegal (out-of-enum) values are
rejected in both conditions — the ablation removes the policy guardrail, not
input validation. Nothing else differs between the two conditions.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, fields
from typing import Any, Dict, List, Optional

from framework.interventions import InterventionSpec, ProposedChange, decide
from framework.run_record import Refinement, RunRecord

APPLIED_BY_GUARDED = "refiner"
APPLIED_BY_UNGUARDED = "refiner-unguarded"

# The ``disclosed`` provenance flag on Refinement is optional in the record
# schema; only set it when the dataclass declares it so an older run_record
# module still works.
_REFINEMENT_HAS_DISCLOSED = any(f.name == "disclosed" for f in fields(Refinement))


@dataclass
class RefineOutcome:
    """Result of applying a batch of proposed changes to a config."""

    next_config: Dict[str, Any]          # config with permitted changes applied
    applied: List[Refinement]            # changes written into next_config
    blocked: List[Refinement]            # Tier C, withheld for human sign-off
    rejected: List[Refinement]           # illegal values, never applied
    enforced: bool = True                # False in the unguarded ablation

    @property
    def needs_human(self) -> bool:
        return bool(self.blocked)


def _new_refinement(
    *,
    tier: str,
    change: Dict[str, Any],
    rationale: str,
    applied: bool,
    applied_by: str,
    disclosed: Optional[bool],
) -> Refinement:
    entry = Refinement(
        tier=tier, change=change, rationale=rationale, applied=applied, applied_by=applied_by,
    )
    if _REFINEMENT_HAS_DISCLOSED:
        entry.disclosed = disclosed
    return entry


def apply_refinements(
    record: RunRecord,
    proposals: List[ProposedChange],
    spec: InterventionSpec,
    enforce: bool = True,
) -> RefineOutcome:
    """Classify and (where permitted) apply a batch of proposed changes.

    Returns a new config rather than mutating ``record.config``; the caller
    runs it as a child run (``parent_run = record.config_hash``). The record's
    ``refinement_history`` is appended to for the full audit trail.

    Proposals may be plain :class:`ProposedChange` or richer objects carrying
    ``rationale`` / ``disclosed`` attributes (e.g. the driver's ``Proposal``);
    those are written into the audit entry alongside the framework's ruling.

    ``enforce=False`` selects the unguarded ablation: legal Tier-C proposals
    are applied and recorded as such, ``blocked`` is always empty, and every
    entry is stamped ``applied_by="refiner-unguarded"``.
    """
    next_config = copy.deepcopy(record.config)
    applied: List[Refinement] = []
    blocked: List[Refinement] = []
    rejected: List[Refinement] = []
    applied_by = APPLIED_BY_GUARDED if enforce else APPLIED_BY_UNGUARDED

    for p in proposals:
        # Ensure 'before' reflects the actual current value.
        before = next_config.get(p.key, p.before)
        change = ProposedChange(key=p.key, before=before, after=p.after)
        # The config is passed so cross-key transition rules (a horizon change
        # while a policy cap is set, say) can see the context they are about.
        decision = decide(change, spec, config=next_config)

        proposer_rationale = str(getattr(p, "rationale", "") or "")
        rationale = decision.reason
        if proposer_rationale:
            rationale = f"{proposer_rationale} — framework: {decision.reason}"

        entry = _new_refinement(
            tier=decision.tier.value,
            change={p.key: [before, p.after]},
            rationale=rationale,
            applied=False,
            applied_by=applied_by,
            disclosed=getattr(p, "disclosed", None),
        )

        if decision.rejected:
            rejected.append(entry)
        elif decision.auto_apply:
            next_config[p.key] = p.after
            entry.applied = True
            applied.append(entry)
        elif not enforce:
            # Unguarded ablation: a legal policy-grade change goes straight in.
            # The tier stays "C" so the violation is visible in the audit trail.
            next_config[p.key] = p.after
            entry.applied = True
            entry.rationale += " [GUARDRAIL DISABLED: applied anyway by refiner-unguarded]"
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
        enforced=enforce,
    )
