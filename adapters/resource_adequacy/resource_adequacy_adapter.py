"""Adapter for Power-Lab/JEPO_ResourceAdequacy_2026."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from adapters.julia_legacy import archive_csvs, output_map, run_julia, write_config
from framework.adapter import Adapter, ValidationResult
from framework.interventions import InterventionSpec
from framework.run_record import Execution

FRAMEWORK_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_MODEL_ROOT = FRAMEWORK_ROOT / "models" / "resource_adequacy"
STUDIES = ("2030_full_factorial",)


class ResourceAdequacyAdapter(Adapter):
    name = "resource_adequacy"

    def __init__(self, model_root: str | Path = DEFAULT_MODEL_ROOT,
                 julia: str = "julia", wall_timeout: Optional[float] = None):
        self.model_root = Path(model_root).resolve()
        self.julia = julia
        self.wall_timeout = wall_timeout

    @property
    def study_root(self) -> Path:
        return self.model_root / "uced_neg_2030" / "uced-model"

    def validate_config(self, config: Dict[str, Any]) -> ValidationResult:
        errors: List[str] = []
        unknown = set(config) - {"study"}
        if unknown:
            errors.append("unsupported keys for the current batch entry point: " + ", ".join(sorted(unknown)))
        if config.get("study") not in STUDIES:
            errors.append("study must be one of: " + ", ".join(STUDIES))
        if not (self.study_root / "R1_Run.jl").is_file():
            errors.append(f"model runner not found: {self.study_root / 'R1_Run.jl'}")
        return ValidationResult(ok=not errors, errors=errors)

    def run(self, config: Dict[str, Any], run_dir: Path,
            on_event: Optional[Callable[..., Any]] = None) -> Execution:
        run_dir = Path(run_dir).resolve()
        write_config(config, run_dir)
        execution, _ = run_julia(
            [self.julia, str(self.study_root / "R1_Run.jl")], self.study_root, run_dir,
            on_event=on_event, timeout=self.wall_timeout,
        )
        # The upstream script controls its own batch result paths. Archive only
        # bounded summaries that changed during this run.
        for candidate in (self.study_root / "Batch", self.study_root / "Results", self.study_root / "results"):
            if candidate.is_dir():
                archive_csvs(candidate.rglob("*.csv"), run_dir)
        return execution

    def intervention_spec(self) -> InterventionSpec:
        return InterventionSpec(
            tier_b_keys={"study"},
            allowed_values={"study": list(STUDIES)},
        )

    def locate_outputs(self, run_dir: Path) -> Dict[str, Path]:
        return output_map(run_dir)

    def describe_config(self) -> str:
        return ("Northeast China resource-adequacy UCED model. The current upstream "
                "entry point runs its complete 3x3x2x3 2030 factorial matrix. The only "
                "accepted config is {'study': '2030_full_factorial'}; per-case controls "
                "are rejected until the Julia runner supports them.")
