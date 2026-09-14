"""Adapter for Power-Lab/EnergyEcon_Storage_2026."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from adapters.julia_legacy import archive_csvs, output_map, run_julia, write_config
from framework.adapter import Adapter, ValidationResult
from framework.interventions import InterventionSpec
from framework.run_record import Execution

FRAMEWORK_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_MODEL_ROOT = FRAMEWORK_ROOT / "models" / "energy_econ_storage"
REQUIRED_KEYS = ("storage_gw", "duration_hours", "wind_scale", "solar_scale",
                 "simulation_days", "production_incentive")


class EnergyStorageAdapter(Adapter):
    name = "storage"

    def __init__(self, model_root: str | Path = DEFAULT_MODEL_ROOT,
                 julia: str = "julia", wall_timeout: Optional[float] = None):
        self.model_root = Path(model_root).resolve()
        self.julia = julia
        self.wall_timeout = wall_timeout

    @staticmethod
    def run_name(config: Dict[str, Any]) -> str:
        def fmt(value: Any) -> str:
            return str(int(value)) if isinstance(value, float) and value.is_integer() else str(value)
        return (f"b{fmt(config['storage_gw'])}_hrs{fmt(config['duration_hours'])}_"
                f"w{fmt(config['wind_scale'])}_s{fmt(config['solar_scale'])}_"
                f"days{fmt(config['simulation_days'])}_ptc{fmt(config['production_incentive'])}")

    def validate_config(self, config: Dict[str, Any]) -> ValidationResult:
        errors: List[str] = []
        missing = set(REQUIRED_KEYS) - set(config)
        unknown = set(config) - set(REQUIRED_KEYS)
        if missing:
            errors.append("missing required keys: " + ", ".join(sorted(missing)))
        if unknown:
            errors.append("unsupported keys: " + ", ".join(sorted(unknown)))
        for key in ("storage_gw", "duration_hours", "wind_scale", "solar_scale", "simulation_days"):
            value = config.get(key)
            if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0):
                errors.append(f"{key} must be a positive number")
        incentive = config.get("production_incentive")
        if incentive is not None and (isinstance(incentive, bool) or not isinstance(incentive, (int, float))):
            errors.append("production_incentive must be numeric")
        runner = self.model_root / "code" / "run_all_periods.jl"
        if not runner.is_file():
            errors.append(f"model runner not found: {runner}")
        return ValidationResult(ok=not errors, errors=errors)

    def run(self, config: Dict[str, Any], run_dir: Path,
            on_event: Optional[Callable[..., Any]] = None) -> Execution:
        run_dir = Path(run_dir).resolve()
        write_config(config, run_dir)
        result_root = self.model_root / "result"
        before = {p.resolve(): p.stat().st_mtime_ns for p in result_root.iterdir()} if result_root.is_dir() else {}
        execution, _ = run_julia(
            [self.julia, str(self.model_root / "code" / "run_all_periods.jl"), self.run_name(config)],
            self.model_root, run_dir, on_event=on_event, timeout=self.wall_timeout,
        )
        if result_root.is_dir():
            changed = [p for p in result_root.iterdir()
                       if p.resolve() not in before or p.stat().st_mtime_ns > before[p.resolve()]]
            for result_dir in sorted(changed, key=lambda p: p.stat().st_mtime_ns, reverse=True)[:1]:
                archive_csvs(result_dir.rglob("*.csv"), run_dir)
        return execution

    def intervention_spec(self) -> InterventionSpec:
        return InterventionSpec(
            tier_b_keys=set(REQUIRED_KEYS) - {"production_incentive"},
            tier_c_keys={"production_incentive"},
        )

    def locate_outputs(self, run_dir: Path) -> Dict[str, Path]:
        return output_map(run_dir)

    def describe_config(self) -> str:
        return ("WECC energy-storage bidding model. Required numeric keys: "
                + ", ".join(REQUIRED_KEYS) + ". production_incentive is Tier C; "
                "the upstream entry point runs the full 90-period workflow.")
