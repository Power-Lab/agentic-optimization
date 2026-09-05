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


_TIER_ORDER: Dict[Tier, int] = {Tier.A: 0, Tier.B: 1, Tier.C: 2}


def strictest(*tiers: Tier) -> Tier:
    """The most restrictive of ``tiers`` (A < B < C)."""
    return max(tiers, key=lambda t: _TIER_ORDER[t])


@dataclass
class TransitionRule:
    """A *value-level* tier, for keys whose tier depends on which way they move.

    A key's tier answers "may the agent touch this at all?", but for some keys
    only one *direction* of change relaxes the study: tightening the knob is a
    sanctioned parameter change while loosening the same knob removes a
    modelled restriction (an "always feasible" fallback engine, a scenario flag
    that is the sole gate on a constraint, shrinking the horizon an absolute
    policy cap is written against). A rule escalates the tier of the matching
    transitions only; every other move keeps the key's declared tier. Rules can
    only make a change *stricter*, never laxer, so declaring one can never
    downgrade a Tier C key.

    Matching: ``from_values``/``to_values`` match by value (``None`` = any);
    ``direction`` matches numeric moves ("decrease"/"increase");
    ``when_config_keys`` restricts the rule to configs where at least one of
    those keys is set to a non-null value (the cross-key case — the change is
    only a relaxation while some policy key is active). Conditions combine with
    AND.
    """

    key: str
    tier: Tier = Tier.C
    reason: str = ""
    from_values: Optional[List[Any]] = None
    to_values: Optional[List[Any]] = None
    direction: Optional[str] = None            # "decrease" | "increase"
    when_config_keys: Optional[List[str]] = None

    def matches(self, change: "ProposedChange",
                config: Optional[Dict[str, Any]] = None) -> bool:
        if change.key != self.key:
            return False
        if self.from_values is not None and change.before not in self.from_values:
            return False
        if self.to_values is not None and change.after not in self.to_values:
            return False
        if self.direction is not None:
            before, after = change.before, change.after
            if isinstance(before, bool) or isinstance(after, bool):
                return False
            if not isinstance(before, (int, float)) or not isinstance(after, (int, float)):
                return False
            if self.direction == "decrease" and not after < before:
                return False
            if self.direction == "increase" and not after > before:
                return False
        if self.when_config_keys:
            if config is None:
                # No context: the rule cannot claim its condition holds.
                return False
            if not any(config.get(k) is not None for k in self.when_config_keys):
                return False
        return True


@dataclass
class InterventionSpec:
    """A model adapter's declaration of which config keys map to which tier."""

    tier_a_keys: Set[str] = field(default_factory=set)
    tier_b_keys: Set[str] = field(default_factory=set)
    tier_c_keys: Set[str] = field(default_factory=set)  # policy constraints
    # Optional enumerated legal values per key (e.g. the scenario enum). A
    # proposed value outside this set is rejected outright, regardless of tier.
    allowed_values: Dict[str, List[Any]] = field(default_factory=dict)
    # Optional value-level escalations: transitions of an otherwise auto-applied
    # key that are themselves policy-grade (see :class:`TransitionRule`).
    transition_rules: List["TransitionRule"] = field(default_factory=list)

    def transition_rule_for(self, change: "ProposedChange",
                            config: Optional[Dict[str, Any]] = None
                            ) -> Optional["TransitionRule"]:
        """The strictest declared rule matching ``change`` in ``config``."""
        matching = [r for r in self.transition_rules if r.matches(change, config)]
        if not matching:
            return None
        return max(matching, key=lambda r: _TIER_ORDER[r.tier])

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


def decide(change: ProposedChange, spec: InterventionSpec,
           config: Optional[Dict[str, Any]] = None) -> Decision:
    """Classify a proposed change and rule on whether it may be auto-applied.

    ``config`` is the config the change would be applied to. It is only read by
    cross-key :class:`TransitionRule` conditions (``when_config_keys``); a rule
    with such a condition never fires when the context is unknown, so callers
    that enforce the guardrail (:func:`framework.refine.apply_refinements`)
    always pass it.
    """
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

    # 2) Tier classification: the key's declared tier, escalated when this
    #    particular transition is one the adapter gated by value.
    key_tier = spec.tier_for_key(key)
    rule = spec.transition_rule_for(change, config)
    tier = key_tier if rule is None else strictest(key_tier, rule.tier)

    if rule is not None and tier is not key_tier:
        detail = rule.reason or (
            f"moving it from {change.before!r} to {change.after!r} relaxes the study"
        )
        tail = ("; auto-applied with a flag."
                if tier.auto_applies else
                "; auto-apply refused — human sign-off required.")
        return Decision(
            tier=tier,
            auto_apply=tier.auto_applies,
            rejected=False,
            reason=(f"{key!r} is declared Tier {key_tier.value}, but {detail} "
                    f"(Tier {tier.value} transition){tail}"),
        )

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
