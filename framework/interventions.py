"""The tiered intervention taxonomy — the scientific-integrity guardrail.

This is the framework's central, generalizable contribution. Any controlled
change the refiner wants to apply to a model config is classified into one of
three tiers:

- **Tier A** (auto-apply): pure numerics — solver tolerances, MIP gap, time
  limit, scaling. These change *how* the solver searches, never *what* problem
  it solves, so an agent may apply them autonomously.
- **Tier B** (apply + loud flag): parameter or scenario changes within an
  adapter-declared allow-list. These change the problem but stay inside the
  space the modeler sanctioned; applied automatically but always surfaced.
- **Tier C** (human sign-off required, never silent): anything that *relaxes a
  policy constraint* (e.g. loosening a CO2 cap, a renewable-share floor, or a
  coal restriction). An agent that "fixes" an infeasible model by quietly
  relaxing the carbon cap produces a feasible-but-scientifically-wrong answer.
  The framework refuses to auto-apply these.

The taxonomy here is model-agnostic. Which config keys fall in which tier is
declared by each model's adapter via :class:`InterventionSpec`. An *unknown*
key defaults to Tier C — the safe default is to never silently touch something
the adapter did not explicitly sanction.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Set


class Tier(str, Enum):
    A = "A"  # numerics — auto-apply
    B = "B"  # sanctioned parameters — apply + flag
    C = "C"  # policy relaxation — human sign-off required

    @property
    def auto_applies(self) -> bool:
        return self in (Tier.A, Tier.B)


@dataclass
class InterventionSpec:
    """A model adapter's declaration of which config keys map to which tier."""

    tier_a_keys: Set[str] = field(default_factory=set)
    tier_b_keys: Set[str] = field(default_factory=set)
    tier_c_keys: Set[str] = field(default_factory=set)  # policy constraints
    # Optional enumerated legal values per key (e.g. the scenario enum). A
    # proposed value outside this set is rejected outright, regardless of tier.
    allowed_values: Dict[str, List[Any]] = field(default_factory=dict)

    def tier_for_key(self, key: str) -> Tier:
        if key in self.tier_c_keys:
            return Tier.C
        if key in self.tier_b_keys:
            return Tier.B
        if key in self.tier_a_keys:
            return Tier.A
        # Unknown key: safest is to treat as policy-grade — never silent.
        return Tier.C


@dataclass
class ProposedChange:
    key: str
    before: Any
    after: Any


@dataclass
class Decision:
    """The framework's ruling on a single proposed change."""

    tier: Tier
    auto_apply: bool      # may the agent apply this without a human?
    rejected: bool        # illegal value — must not be applied at all
    reason: str

    @property
    def blocked_for_human(self) -> bool:
        """True when the change is legal but withheld pending human sign-off."""
        return not self.auto_apply and not self.rejected


def decide(change: ProposedChange, spec: InterventionSpec) -> Decision:
    """Classify a proposed change and rule on whether it may be auto-applied."""
    key = change.key

    # 1) Legality: an out-of-enum value is never applied, whatever its tier.
    allowed = spec.allowed_values.get(key)
    if allowed is not None and change.after not in allowed:
        return Decision(
            tier=spec.tier_for_key(key),
            auto_apply=False,
            rejected=True,
            reason=(
                f"Value {change.after!r} for {key!r} is not in the adapter's "
                f"allowed set {allowed!r}; rejected."
            ),
        )

    # 2) Tier classification.
    tier = spec.tier_for_key(key)
    if tier is Tier.A:
        reason = f"{key!r} is a numeric/solver setting (Tier A); auto-applied."
    elif tier is Tier.B:
        reason = f"{key!r} is a sanctioned parameter (Tier B); auto-applied with a flag."
    else:
        if key in spec.tier_c_keys:
            reason = (
                f"{key!r} is a policy constraint (Tier C); auto-apply refused — "
                f"relaxing it would change the study's meaning. Human sign-off required."
            )
        else:
            reason = (
                f"{key!r} is not in any declared tier; defaulting to Tier C. "
                f"Auto-apply refused — the agent will not silently touch an "
                f"unsanctioned key."
            )

    return Decision(
        tier=tier,
        auto_apply=tier.auto_applies,
        rejected=False,
        reason=reason,
    )
