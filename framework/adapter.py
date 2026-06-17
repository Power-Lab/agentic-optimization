"""The adapter interface every model implements to plug into the framework.

The framework core (run record, intervention taxonomy, supervisor loop) knows
nothing model-specific. A concrete model joins by subclassing :class:`Adapter`
and wiring these four methods to its own scenario/run/output machinery. The
village-Indonesia capacity-expansion model is the reference adapter; a second
adapter (e.g. a PyPSA/GenX toy) is what would prove generality for a paper.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List

from framework.interventions import InterventionSpec
from framework.run_record import Execution


@dataclass
class ValidationResult:
    ok: bool
    errors: List[str] = field(default_factory=list)


class Adapter(ABC):
    """Contract between the framework and a concrete optimization model."""

    #: short, stable identifier (e.g. "village")
    name: str = "adapter"

    @abstractmethod
    def validate_config(self, config: Dict[str, Any]) -> ValidationResult:
        """Cheap, side-effect-free preflight: are required keys present, do the
        referenced inputs exist, is the scenario legal? Used as a gate before an
        expensive run and by the scenario-builder skill."""

    @abstractmethod
    def run(self, config: Dict[str, Any], run_dir: Path) -> Execution:
        """Execute one optimization run.

        Must: write ``config.json`` into ``run_dir``, invoke the model, capture
        the solver's stdout to ``run_dir/solver.log``, and return an
        :class:`Execution` with the termination status and timing. Must not
        raise on an infeasible/failed solve — that is a normal Execution result
        the analyzer will read, not an exception."""

    @abstractmethod
    def intervention_spec(self) -> InterventionSpec:
        """Declare which config keys fall in Tier A/B/C and any enumerated legal
        values. This is the model's contribution to the safety guardrail."""

    @abstractmethod
    def locate_outputs(self, run_dir: Path) -> Dict[str, Path]:
        """Map a stable metric/output name to the file that holds it, so the
        output-analyzer does not hard-code this model's filenames."""

    def describe_config(self) -> str:
        """Human-readable description of this model's config schema and levers,
        so the (model-agnostic) scenario-builder/refiner skills can learn the
        model from the adapter instead of hard-coding it. Default derives a
        summary from the intervention spec; adapters should override with a
        fuller description of keys and scenario semantics."""
        spec = self.intervention_spec()
        return (
            f"Adapter '{self.name}'. Intervention tiers — "
            f"A/auto (numerics): {sorted(spec.tier_a_keys)}; "
            f"B/auto+flag (params): {sorted(spec.tier_b_keys)}; "
            f"C/human-only (policy): {sorted(spec.tier_c_keys)}. "
            f"Enumerated legal values: {spec.allowed_values or '{}'}."
        )
