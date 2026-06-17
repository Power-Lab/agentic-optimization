"""Reference adapter: the village-Indonesia 100 GW capacity-expansion model.

Wires the framework's four-method contract to the model living under
``models/village`` (a git submodule, never modified):

- config           -> the model's ``config.json`` schema
- run              -> ``julia --project=. run_model.jl --config <cfg>``
- intervention tiers -> the scenario enum in ``functions/preflight.jl`` plus the
                       documented optional passthrough keys
- outputs          -> the per-run CSVs under the model's ``results/`` folder

Nothing here edits the model. Solver output is captured by teeing the Julia
subprocess stdout (which already carries Gurobi's iteration log and the
termination ``println``) into ``solver.log``.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any, Dict, List

from framework.adapter import Adapter, ValidationResult
from framework.interventions import InterventionSpec
from framework.run_record import Execution

# Repo layout: <framework-root>/adapters/village/village_adapter.py
FRAMEWORK_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_MODEL_ROOT = FRAMEWORK_ROOT / "models" / "village"

# Mirrors functions/preflight.jl::load_config
REQUIRED_KEYS = [
    "island",
    "year",
    "scenario",
    "clean",
    "CO235reduction",
    "BAUCO2emissions",
    "CO2_limit",
]

# Mirrors functions/preflight.jl::scenario_settings (+ legacy aliases)
SCENARIO_ENUM = [
    "base",
    "grid",
    "village",
    "gridvillage",
    "highimportprice",
    "nocoal",
    "captive",       # legacy alias for village
    "gridcaptive",   # legacy alias for gridvillage
]
CLEAN_ENUM = ["reference", "clean"]

# Documented optional passthrough keys (generate_jobs_local.py:90, run_model.jl)
PASSTHROUGH_KEYS = ["mipgap", "RE_limit", "import_price", "village_storage_max_mwh"]

# Maps the termination println strings in optimizer.jl to canonical statuses.
_STATUS_MARKERS = {
    "The model solved successfully.": "OPTIMAL",
    "The model reached the time limit.": "TIME_LIMIT",
    "The model is infeasible.": "INFEASIBLE",
}


class VillageAdapter(Adapter):
    name = "village"

    def __init__(self, model_root: str | Path = DEFAULT_MODEL_ROOT, julia: str = "julia"):
        self.model_root = Path(model_root).resolve()
        self.julia = julia

    # ---- paths ---------------------------------------------------------

    def inputs_path(self, config: Dict[str, Any]) -> Path:
        return self.model_root / "data_indonesia" / str(config["year"]) / str(config["island"])

    def results_name(self, config: Dict[str, Any]) -> str:
        return f"{config['scenario']}_{config['island']}_{config['year']}_{config['clean']}"

    def model_results_dir(self, config: Dict[str, Any]) -> Path:
        # run_model.jl always writes here, relative to the model root.
        return self.model_root / "results" / self.results_name(config)

    # ---- adapter interface --------------------------------------------

    @staticmethod
    def validate_schema(config: Dict[str, Any]) -> List[str]:
        """Filesystem-free schema checks (keys + enums). Shared by local and
        remote adapters."""
        errors: List[str] = []

        missing = [k for k in REQUIRED_KEYS if k not in config]
        if missing:
            errors.append(f"Missing required config keys: {', '.join(missing)}")

        scenario = config.get("scenario")
        if scenario is not None and scenario not in SCENARIO_ENUM:
            errors.append(
                f"Unknown scenario {scenario!r}; legal values: {', '.join(SCENARIO_ENUM)}"
            )

        clean = config.get("clean")
        if clean is not None and clean not in CLEAN_ENUM:
            errors.append(f"Unknown clean flag {clean!r}; legal values: {', '.join(CLEAN_ENUM)}")

        return errors

    def validate_config(self, config: Dict[str, Any]) -> ValidationResult:
        """Fast, Gurobi-free preflight (the heavier julia --preflight-only gate
        is available via :meth:`julia_preflight`)."""
        errors = self.validate_schema(config)

        if "island" in config and "year" in config:
            ip = self.inputs_path(config)
            if not ip.is_dir():
                errors.append(f"Input data directory not found: {ip}")

        return ValidationResult(ok=not errors, errors=errors)

    def julia_preflight(self, config: Dict[str, Any], run_dir: Path) -> subprocess.CompletedProcess:
        """Run the model's own ``--preflight-only`` gate (also checks Gurobi)."""
        cfg_path = self._write_config(config, run_dir)
        return subprocess.run(
            [self.julia, f"--project={self.model_root}", "run_model.jl",
             "--config", str(cfg_path), "--preflight-only"],
            cwd=self.model_root, capture_output=True, text=True,
        )

    def run(self, config: Dict[str, Any], run_dir: Path) -> Execution:
        run_dir = Path(run_dir)
        cfg_path = self._write_config(config, run_dir)
        log_path = run_dir / "solver.log"

        start = time.monotonic()
        proc = subprocess.run(
            [self.julia, f"--project={self.model_root}", "run_model.jl",
             "--config", str(cfg_path)],
            cwd=self.model_root, capture_output=True, text=True,
        )
        wall = time.monotonic() - start

        combined = proc.stdout + ("\n[stderr]\n" + proc.stderr if proc.stderr else "")
        log_path.write_text(combined)

        status, origin = self._parse_status(combined, proc.returncode)

        # Archive the model's CSV outputs into the run dir for an immutable
        # record (the model writes them into its own results/ tree).
        self._archive_outputs(config, run_dir)

        return Execution(
            termination_status=status,
            wall_seconds=round(wall, 1),
            mipgap_reached=None,
            solver_log="solver.log",
            returncode=proc.returncode,
            error_origin=origin,
        )

    def intervention_spec(self) -> InterventionSpec:
        return InterventionSpec(
            # Tier A — numerics that change the search, not the problem.
            tier_a_keys={"mipgap"},
            # Tier B — sanctioned parameters / scenario choice within the enum.
            tier_b_keys={"scenario", "import_price", "village_storage_max_mwh"},
            # Tier C — policy constraints; relaxing them changes the study.
            tier_c_keys={"clean", "CO2_limit", "RE_limit", "CO235reduction", "BAUCO2emissions"},
            allowed_values={"scenario": SCENARIO_ENUM, "clean": CLEAN_ENUM},
        )

    def locate_outputs(self, run_dir: Path) -> Dict[str, Path]:
        out_dir = Path(run_dir) / "outputs"
        if not out_dir.is_dir():
            return {}
        return {p.stem: p for p in sorted(out_dir.glob("*.csv"))}

    def describe_config(self) -> str:
        return (
            "Village-Indonesia capacity-expansion model (case study).\n"
            f"Required keys: {', '.join(REQUIRED_KEYS)}.\n"
            f"Legal `scenario`: {', '.join(SCENARIO_ENUM)} "
            "(captive/gridcaptive are legacy aliases). Semantics: base=no grid, "
            "no village build; village=standalone village build; grid=grid "
            "expansion only; gridvillage=coordinated grid+village; nocoal=coal "
            "banned; highimportprice=higher village import price.\n"
            f"Legal `clean`: {', '.join(CLEAN_ENUM)} (clean enforces the CO2 cap "
            "and RE-share floor — a POLICY lever).\n"
            f"Optional passthrough: {', '.join(PASSTHROUGH_KEYS)} "
            "(mipgap=numeric; import_price/village_storage_max_mwh=parameters; "
            "RE_limit=policy floor).\n"
            "There is no direct 'X% solar' knob — penetration is an outcome of "
            "costs/resources, not a config key."
        )

    # ---- helpers -------------------------------------------------------

    def _write_config(self, config: Dict[str, Any], run_dir: Path) -> Path:
        import json

        run_dir = Path(run_dir)
        run_dir.mkdir(parents=True, exist_ok=True)
        cfg_path = run_dir / "config.json"
        cfg_path.write_text(json.dumps(config, indent=2))
        return cfg_path

    def _archive_outputs(self, config: Dict[str, Any], run_dir: Path) -> None:
        src = self.model_results_dir(config)
        if not src.is_dir():
            return
        dst = Path(run_dir) / "outputs"
        dst.mkdir(parents=True, exist_ok=True)
        for csv in src.glob("*.csv"):
            shutil.copy2(csv, dst / csv.name)

    @staticmethod
    def _parse_status(output: str, returncode: int):
        for marker, status in _STATUS_MARKERS.items():
            if marker in output:
                return status, None
        # The model prints the raw MOI termination status on the "else" branch.
        # Gurobi reports INFEASIBLE_OR_UNBOUNDED by default (it does not
        # disambiguate without DualReductions=0); treat any infeasible/unbounded
        # token as a solver-level infeasibility, even though the model then
        # crashes trying to extract a nonexistent objective value.
        lowered = output.lower()
        if "infeasible" in lowered or "unbounded" in lowered:
            return "INFEASIBLE", "solver"
        if "did not solve successfully" in output:
            return "ERROR", "solver"
        if "Preflight checks passed" in output:
            return "PREFLIGHT_OK", None
        if returncode != 0:
            origin = "preflight" if "preflight" in lowered else "runtime"
            return "ERROR", origin
        return "UNKNOWN", None
