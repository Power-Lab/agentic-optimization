"""Adapter for Power-Lab/captive-indonesia-2025."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from adapters.julia_legacy import archive_csvs, output_map, run_julia, write_config
from framework.adapter import Adapter, ValidationResult
from framework.interventions import InterventionSpec
from framework.run_record import Execution

FRAMEWORK_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_MODEL_ROOT = FRAMEWORK_ROOT / "models" / "captive_indonesia"
REQUIRED_KEYS = ("island", "year", "scenario", "clean", "CO235reduction",
                 "BAUCO2emissions", "CO2_limit")
SCENARIOS = ("base", "gridcaptive", "grid", "captive", "highimportprice", "nocoal")
CLEAN_VALUES = ("reference", "clean")


class CaptiveIndonesiaAdapter(Adapter):
    name = "captive"

    def __init__(self, model_root: str | Path = DEFAULT_MODEL_ROOT,
                 julia: str = "julia", wall_timeout: Optional[float] = None):
        self.model_root = Path(model_root).resolve()
        self.julia = julia
        self.wall_timeout = wall_timeout

    def validate_config(self, config: Dict[str, Any]) -> ValidationResult:
        errors: List[str] = []
        unknown = set(config) - set(REQUIRED_KEYS)
        missing = set(REQUIRED_KEYS) - set(config)
        if missing:
            errors.append("missing required keys: " + ", ".join(sorted(missing)))
        if unknown:
            errors.append("unsupported keys: " + ", ".join(sorted(unknown)))
        if config.get("scenario") not in SCENARIOS:
            errors.append("scenario must be one of: " + ", ".join(SCENARIOS))
        if config.get("clean") not in CLEAN_VALUES:
            errors.append("clean must be reference or clean")
        if "year" in config and "island" in config:
            inputs = self.model_root / "data_indonesia" / str(config["year"]) / str(config["island"])
            if not inputs.is_dir():
                errors.append(f"input data directory not found: {inputs}")
        if not (self.model_root / "run_model.jl").is_file():
            errors.append(f"model runner not found: {self.model_root / 'run_model.jl'}")
        return ValidationResult(ok=not errors, errors=errors)

    def run(self, config: Dict[str, Any], run_dir: Path,
            on_event: Optional[Callable[..., Any]] = None) -> Execution:
        run_dir = Path(run_dir).resolve()
        write_config(config, run_dir)
        execution, _ = run_julia(
            [self.julia, str(self.model_root / "run_model.jl")], run_dir, run_dir,
            on_event=on_event, timeout=self.wall_timeout,
        )
        result_dir = self.model_root / "results" / (
            f"{config['scenario']}_{config['island']}_{config['year']}_{config['clean']}"
        )
        if result_dir.is_dir():
            archive_csvs(result_dir.glob("*.csv"), run_dir)
        return execution

    def intervention_spec(self) -> InterventionSpec:
        return InterventionSpec(
            tier_b_keys={"island", "year", "scenario"},
            tier_c_keys={"clean", "CO235reduction", "BAUCO2emissions", "CO2_limit"},
            allowed_values={"scenario": list(SCENARIOS), "clean": list(CLEAN_VALUES)},
        )

    def locate_outputs(self, run_dir: Path) -> Dict[str, Path]:
        return output_map(run_dir)

    def describe_config(self) -> str:
        return ("Captive Indonesia capacity-expansion model. Required keys: "
                + ", ".join(REQUIRED_KEYS) + ". Emissions and clean-policy values "
                "are Tier C and require human approval.")
