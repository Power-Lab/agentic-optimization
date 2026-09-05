"""Reference adapter: the garuda zonal capacity-expansion / dispatch platform.

Wires the framework's adapter contract to the model living under
``models/garuda`` (a git submodule, never modified):

- config             -> the model's ``config.json`` schema (``functions/preflight.jl``
                        + the optional keys read by ``run_model.jl``)
- run                -> ``julia --project=<root> run_model.jl --config <cfg>``, streamed
- intervention tiers -> the modeler-approved A/B/C declaration below
- outputs            -> the per-run CSVs under the model's ``results/`` folder,
                        archived into ``<run_dir>/outputs/``

Nothing here edits the model. Solver output (HiGHS/Gurobi iteration log plus the
engine's termination ``println``) is teed line-by-line into ``solver.log`` as the
Julia subprocess produces it, so a run can be watched while it solves.
"""

from __future__ import annotations

import csv
import json
import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass, fields
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

from framework.adapter import Adapter, ValidationResult
from framework.interventions import InterventionSpec, Tier, TransitionRule
from framework.run_record import Execution

try:  # the framework's streamer: tee + ticks + abort file, shared by all adapters
    from framework.process import stream_command
except ImportError:  # pragma: no cover - only if framework/process.py is missing
    stream_command = None  # type: ignore[assignment]

# Repo layout: <framework-root>/adapters/garuda/garuda_adapter.py
FRAMEWORK_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_MODEL_ROOT = FRAMEWORK_ROOT / "models" / "garuda"
MODEL_REPO_URL = "https://github.com/kaarthi19/garuda.git"

# The model's preflight shells out to tools/validate_schema.py with $GARUDA_PYTHON
# (default python3). That validator needs pandas; a python without it exits 3 and
# the model skips validation with a warning. Prefer an interpreter that has it.
_MINIFORGE_PYTHON = Path.home() / "miniforge3" / "bin" / "python"


def default_validator_python() -> str:
    env = os.environ.get("GARUDA_PYTHON")
    if env:
        return env
    if _MINIFORGE_PYTHON.exists():
        return str(_MINIFORGE_PYTHON)
    return "python3"


# ---- schema (mirrors functions/preflight.jl + run_model.jl) ---------------

REQUIRED_KEYS = [
    "island",
    "year",
    "scenario",
    "clean",
    "CO235reduction",
    "BAUCO2emissions",
    "CO2_limit",
]

# preflight.jl::scenario_settings (+ legacy aliases)
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
ENGINE_ENUM = ["expansion", "dispatch"]
SOLVER_ENUM = ["highs", "gurobi"]
POLICY_SCOPE_ENUM = ["grid", "system"]
LP_METHOD_VALUES = list(range(-1, 6))  # Gurobi Method: -1 auto .. 5

# preflight.jl::scenario_settings — what each scenario switches on.
SCENARIO_FLAGS = {
    "base":            dict(Grid=False, VillageBuild=False, ImportPrice=59.0, NoCoal=False),
    "grid":            dict(Grid=True,  VillageBuild=False, ImportPrice=59.0, NoCoal=False),
    "village":         dict(Grid=False, VillageBuild=True,  ImportPrice=59.0, NoCoal=False),
    "gridvillage":     dict(Grid=True,  VillageBuild=True,  ImportPrice=59.0, NoCoal=False),
    "highimportprice": dict(Grid=True,  VillageBuild=True,  ImportPrice=59.0 * 1.21, NoCoal=False),
    "nocoal":          dict(Grid=True,  VillageBuild=False, ImportPrice=59.0, NoCoal=True),
    "captive":         dict(Grid=False, VillageBuild=True,  ImportPrice=59.0, NoCoal=False),
    "gridcaptive":     dict(Grid=True,  VillageBuild=True,  ImportPrice=59.0, NoCoal=False),
}

# preflight.jl: accepted spellings of the site tables (canonical first).
_SITE_PREFIXES = ("site_", "village_", "ip_")

_NUMBER = (int, float)


@dataclass(frozen=True)
class KeySpec:
    """One optional config key: how to validate it and how to explain it."""

    name: str
    tier: str                     # "A" | "B" | "C"
    kind: str                     # "number" | "int" | "bool" | "str" | "enum"
    default: Any
    doc: str
    enum: Optional[List[Any]] = None
    minimum: Optional[float] = None
    exclusive_minimum: bool = False


# Every config key run_model.jl reads, with the modeler-approved tier — the
# optional ones *and* the five required ones (scenario, clean, CO2_limit,
# CO235reduction, BAUCO2emissions), which are declared here so they carry a tier
# and a doc; describe_config() filters those out of the "optional" listing. No
# count is quoted on purpose: re-sync by diffing this list against the keys
# run_model.jl actually reads, not against a number that goes stale.
OPTIONAL_KEYS: List[KeySpec] = [
    KeySpec("mipgap", "A", "number", 0.01,
            "relative MIP gap the solver stops at (HiGHS mip_rel_gap / Gurobi MIPGap). "
            "Search setting only; irrelevant to a pure LP (dispatch or relax_uc).",
            minimum=0.0),
    KeySpec("time_limit", "A", "number", 259200.0,
            "solver wall-clock limit in seconds (default 3 days). On expiry the engine prints "
            "'reached the time limit' and still extracts the incumbent, so a bounded MILP run "
            "yields a usable plan plus a reported gap. Prefer this to an external kill, which "
            "destroys the result CSVs.",
            minimum=0.0, exclusive_minimum=True),
    KeySpec("solver", "A", "enum", "highs",
            "'highs' (open-source, no licence; fine for LPs, slow on big MILPs) or 'gurobi' "
            "(needs a licence; ~5 s for the timor_demo expansion MILP vs ~25 min on HiGHS).",
            enum=SOLVER_ENUM),
    KeySpec("lp_method", "A", "int", -1,
            "Gurobi 'Method' for the root/node LPs: -1 auto, 0 primal simplex, 1 dual simplex, "
            "2 barrier, 3 concurrent, 4/5 deterministic concurrent. Ignored (with a warning) "
            "by HiGHS.",
            enum=LP_METHOD_VALUES),
    KeySpec("run_tag", "A", "str", "",
            "suffix for the model's results folder "
            "(results/<scenario>_<island>_<year>_<clean>__<run_tag>/). Labelling only. The "
            "adapter injects one (the run directory's basename) when the config has none, so "
            "concurrent runs never overwrite each other's results folder."),
    KeySpec("scenario", "B", "enum", None,
            "REQUIRED. Which scenario configuration to solve (see scenario semantics). "
            "Tier B as a *choice*, but two of its values carry a modelled restriction "
            "rather than a parameter: 'nocoal' is the only switch on the renewables-only "
            "village electricity balance, and 'highimportprice' is the only switch on the "
            "x1.21 import price. Moving AWAY from either drops that restriction, so those "
            "transitions are gated at Tier C (see TRANSITION_RULES / the value-level "
            "escalations listed below).",
            enum=SCENARIO_ENUM),
    KeySpec("engine", "B", "enum", "expansion",
            "'expansion' co-optimises investment + operation (MILP with exact unit commitment "
            "by default); 'dispatch' fixes capacity to the existing fleet (New_Build units "
            "at 0) and solves only operations (LP by default). BOTH engines share "
            "build_model!: the same non-served-energy slack at VOLL (vNSE / vVIL_NSE, capped "
            "by cMaxNSE) and the same CO2 cap / RE floor, so switching engine neither "
            "relaxes demand nor a policy constraint — an expansion case that is infeasible "
            "under a cap stays infeasible in dispatch. Tier B in both directions.",
            enum=ENGINE_ENUM),
    KeySpec("relax_uc", "B", "bool", None,
            "LP-relax the unit-commitment (and village grid-connection) binaries. Default true "
            "for dispatch, false for expansion. true on expansion = fast licence-free LP that "
            "is an approximation (~0.8 % below the exact MILP cost on timor_demo)."),
    KeySpec("exact_connect", "B", "bool", False,
            "with relax_uc, keep the village grid-connection binaries exact while the UC "
            "binaries relax (avoids fractional 'Connected' artefacts). No-op when relax_uc is "
            "false."),
    KeySpec("import_price", "B", "number", 59.0,
            "$/MWh sites pay for grid imports (scenario default 59.0; 71.39 for "
            "highimportprice).",
            minimum=0.0),
    KeySpec("export_price", "B", "number", 0.0,
            "$/MWh feed-in price sites earn for exports; 0 = exports are unremunerated spill. "
            "Keep <= import_price: a higher export price lets a connected site profit from "
            "importing and re-exporting. On a dataset whose grid zone has demand the model "
            "only prints 'WARNING: export_price (...) > import_price (...)' and the results "
            "are distorted by that arbitrage; on a dataset with NO grid demand it refuses "
            "the configuration outright (optimizer.jl raises 'export_price (...) > "
            "import_price (...) on a dataset with NO grid demand') and the run ends as "
            "ERROR/preflight — unless export_backed_by_generation is true.",
            minimum=0.0),
    KeySpec("village_storage_max_mwh", "B", "number", 208.0,
            "per-unit cap (MWh) on new site storage energy.",
            minimum=0.0),
    KeySpec("battery_duration_h", "B", "number", 0.0,
            "fix new site storage to a duration (energy = hours x power). 0 = power and energy "
            "co-optimised independently.",
            minimum=0.0),
    KeySpec("clean", "C", "enum", None,
            "REQUIRED. 'reference' = no policy constraints; 'clean' = enforce the CO2 cap "
            "(CO2_limit) AND the renewable-share floor (RE_limit). A POLICY lever.",
            enum=CLEAN_ENUM),
    KeySpec("CO2_limit", "C", "number", None,
            "REQUIRED. Annual CO2 cap (tCO2) applied to grid emissions when clean='clean' "
            "(system-wide if policy_scope='system'). Emissions are non-negative, so a negative "
            "cap is unsatisfiable. Ignored when clean='reference' (set a large value such as "
            "1e12). POLICY."),
    KeySpec("RE_limit", "C", "number", 0.34,
            "minimum renewable share of grid demand enforced when clean='clean' (0.34 = the "
            "JETP 34 % target). A share above 1.0 is impossible. POLICY.",
            minimum=0.0),
    KeySpec("CO235reduction", "C", "bool", None,
            "REQUIRED. If true, cap village-layer emissions at 65 % of BAUCO2emissions "
            "(a 35 % reduction). POLICY."),
    KeySpec("BAUCO2emissions", "C", "number", None,
            "REQUIRED. Business-as-usual village emissions (tCO2) the CO235reduction cap is "
            "computed from. POLICY.",
            minimum=0.0),
    KeySpec("policy_scope", "C", "enum", "grid",
            "scope of the clean-run constraints: 'grid' (cap + floor on the grid layer) or "
            "'system' (village layer included in both). POLICY.",
            enum=POLICY_SCOPE_ENUM),
    KeySpec("export_backed_by_generation", "C", "bool", False,
            "require each site's hourly export to come from its own renewable generation that "
            "hour (a feed-in-contract rule; makes wash trades structurally impossible). Adds a "
            "row per hour x site. POLICY.")
]

_KEYSPEC_BY_NAME: Dict[str, KeySpec] = {k.name: k for k in OPTIONAL_KEYS}
KNOWN_KEYS = set(REQUIRED_KEYS) | set(_KEYSPEC_BY_NAME)

TIER_A_KEYS = {k.name for k in OPTIONAL_KEYS if k.tier == "A"}
TIER_B_KEYS = {k.name for k in OPTIONAL_KEYS if k.tier == "B"}
TIER_C_KEYS = {k.name for k in OPTIONAL_KEYS if k.tier == "C"}
ALLOWED_VALUES: Dict[str, List[Any]] = {
    k.name: list(k.enum) for k in OPTIONAL_KEYS if k.enum is not None
}

# ---- value-level escalations (the guardrail's second axis) -------------------
# `scenario` is a Tier B *choice*, but two of its transitions remove a modelled
# restriction instead of moving a sanctioned parameter. Those transitions are
# gated at Tier C; entering the restriction stays auto-applied. (`engine` has no
# rule on purpose: both engines share build_model!, incl. the NSE slack and the
# policy constraints, so neither direction relaxes the study.)
_BASE_IMPORT_PRICE = min(f["ImportPrice"] for f in SCENARIO_FLAGS.values())
_NOCOAL_SCENARIOS = sorted(s for s, f in SCENARIO_FLAGS.items() if f["NoCoal"])
_COAL_SCENARIOS = sorted(s for s, f in SCENARIO_FLAGS.items() if not f["NoCoal"])
_HIGH_IMPORT_SCENARIOS = sorted(
    s for s, f in SCENARIO_FLAGS.items() if f["ImportPrice"] > _BASE_IMPORT_PRICE)
_BASE_IMPORT_SCENARIOS = sorted(
    s for s, f in SCENARIO_FLAGS.items() if f["ImportPrice"] <= _BASE_IMPORT_PRICE)

TRANSITION_RULES: List[TransitionRule] = [
    TransitionRule(
        key="scenario", tier=Tier.C,
        from_values=_NOCOAL_SCENARIOS, to_values=_COAL_SCENARIOS,
        reason=("leaving a NoCoal scenario drops the renewables-only village "
                "electricity balance (optimizer.jl cVILElectricityBalance restricted "
                "to inputs.VIL_RE) — the scenario flag is the sole gate on it"),
    ),
    TransitionRule(
        key="scenario", tier=Tier.C,
        from_values=_HIGH_IMPORT_SCENARIOS, to_values=_BASE_IMPORT_SCENARIOS,
        reason=("leaving a high-import-price scenario silently reverts the x1.21 "
                "import-price assumption the case is built on"),
    ),
]

# The model's own CI regression case (.github/ci/maluku_dispatch.config.json) and
# its headline numbers (tests/check_dispatch_headlines.jl, +-1 %). HiGHS, no
# licence, minutes. 12.4 % unserved energy is *expected* on this dataset.
REGRESSION_CONFIG: Dict[str, Any] = {
    "island": "maluku",
    "year": "2030",
    "scenario": "base",
    "clean": "reference",
    "CO235reduction": False,
    "BAUCO2emissions": 0.0,
    "CO2_limit": 1e12,
    "engine": "dispatch",
    "relax_uc": True,
    "solver": "highs",
    "mipgap": 0.01,
}
REGRESSION_HEADLINES: Dict[str, Tuple[str, str, float]] = {
    # metric -> (csv stem, column, expected value); reliability sums the column.
    "Total_Costs":   ("cost_results",         "Total_Costs",   418.75012738446367),
    "CO2_Emissions": ("clean_energy_results", "CO2_Emissions", 889909.6973480765),
    "Total_NSE_MWh": ("reliability_results",  "Total_NSE_MWh", 205997.29702380873),
}
REGRESSION_TOLERANCE = 0.01

# Result CSVs the engines can write (which appear depends on scenario/engine).
RESULT_CSVS = [
    "clean_energy_results", "cost_results", "generator_results", "nse_results",
    "reliability_results", "storage_results", "transmission_results",
    "transmission_flow_results", "site_connection_results", "site_generator_results",
    "site_heat_generator_results", "site_import_results", "site_nse_heat_results",
    "site_nse_results", "site_reliability_results", "site_storage_results",
]

# ---- status parsing --------------------------------------------------------

# Explicit engine status lines (optimizer.jl ~933, dispatch_engine.jl ~55). Order
# matters: an exact marker beats every fallback token below.
_STATUS_MARKERS: List[Tuple[str, str, Optional[str]]] = [
    ("solved successfully", "OPTIMAL", None),
    ("reached the time limit", "TIME_LIMIT", None),
    ("Capacity expansion is infeasible", "INFEASIBLE", "solver"),
    ("Dispatch is infeasible", "INFEASIBLE", "solver"),
    ("did not solve. Termination status", "ERROR", "solver"),
    # optimizer.jl build_model!: export_price > import_price on a dataset with no
    # grid demand is refused outright (a fabricating configuration), after
    # preflight has already printed its OK line.
    ("on a dataset with NO grid demand", "ERROR", "preflight"),
]
_PREFLIGHT_OK_MARKER = "Preflight checks passed"

# Error strings raised by preflight.jl / run_model.jl before any solve.
_PREFLIGHT_ERROR_TOKENS = [
    "Config file not found",
    "missing required keys",
    "Unknown scenario",
    "Unknown clean flag",
    "Unknown solver",
    "Input data directory not found",
    "Missing required input files",
    "Input schema validation failed",
    "could not start a working optimizer",
    "config key",            # run_model.jl range checks (battery_duration_h, lp_method, ...)
    "policy_scope must be",
    "not found in current path",   # `using Gurobi` with Gurobi.jl not installed
]

# "... did not solve. Termination status: X" — X is JuMP's MOI status name.
_TERMINATION_STATUS = re.compile(r"did not solve\. Termination status:\s*([A-Za-z_]+)")

# Workstream D adds ``Execution.monitor``; attach the live-monitor summary only
# when the record schema declares it, so an older framework still works.
_EXECUTION_HAS_MONITOR = any(f.name == "monitor" for f in fields(Execution))

_GUROBI_GAP = re.compile(r"Best objective [^,]+, best bound [^,]+, gap ([0-9.eE+-]+)%")
_HIGHS_GAP = re.compile(r"^\s*Gap\s+([0-9.eE+-]+)%", re.MULTILINE)


class GarudaAdapter(Adapter):
    name = "garuda"

    def __init__(
        self,
        model_root: str | Path = DEFAULT_MODEL_ROOT,
        julia: str = "julia",
        python_for_validator: Optional[str] = None,
        skip_schema_validation: bool = False,
    ):
        self.model_root = Path(model_root).resolve()
        self.julia = julia
        self.python_for_validator = python_for_validator or default_validator_python()
        self.skip_schema_validation = skip_schema_validation

    # ---- paths ---------------------------------------------------------

    def inputs_path(self, config: Dict[str, Any]) -> Path:
        return self.model_root / "data_indonesia" / str(config["year"]) / str(config["island"])

    @staticmethod
    def results_name(config: Dict[str, Any]) -> str:
        """``<scenario>_<island>_<year>_<clean>[__<run_tag>]`` — preflight.jl's rule."""
        base = f"{config['scenario']}_{config['island']}_{config['year']}_{config['clean']}"
        tag = str(config.get("run_tag", "") or "").strip()
        return f"{base}__{tag}" if tag else base

    def model_results_dir(self, config: Dict[str, Any]) -> Path:
        # run_model.jl always writes here, relative to the model root.
        return self.model_root / "results" / self.results_name(config)

    # ---- schema validation (filesystem-free) -----------------------------

    @staticmethod
    def validate_schema(config: Dict[str, Any]) -> List[str]:
        """Key/enum/type/range checks that need no filesystem. Shared by the local
        and remote adapters."""
        errors: List[str] = []
        if not isinstance(config, dict):
            return ["config must be a JSON object"]

        missing = [k for k in REQUIRED_KEYS if k not in config]
        if missing:
            errors.append(f"Missing required config keys: {', '.join(missing)}")

        for key in ("island", "year"):
            if key in config and not isinstance(config[key], str):
                errors.append(
                    f"{key} must be a string (e.g. \"2030\"), got {type(config[key]).__name__}: "
                    f"the model builds the input path from it"
                )

        for key, value in config.items():
            if key in ("island", "year"):
                continue
            spec = _KEYSPEC_BY_NAME.get(key)
            if spec is None:
                errors.append(
                    f"Unknown config key {key!r}: the model silently ignores keys it does not "
                    f"read, so this would solve at the default. Known keys: "
                    f"{', '.join(sorted(KNOWN_KEYS))}"
                )
                continue
            errors.extend(_check_value(spec, value))

        return errors

    def validate_config(self, config: Dict[str, Any]) -> ValidationResult:
        """Fast, solver-free preflight mirroring ``functions/preflight.jl``
        (the model's own heavier gate — which also test-solves a 1-variable
        problem and schema-validates the dataset — is :meth:`julia_preflight`)."""
        errors = self.validate_schema(config)

        if isinstance(config, dict) and isinstance(config.get("island"), str) \
                and isinstance(config.get("year"), str):
            ip = self.inputs_path(config)
            if not ip.is_dir():
                errors.append(f"Input data directory not found: {ip}")
            else:
                scenario = config.get("scenario")
                if scenario in SCENARIO_FLAGS:
                    missing_files = [
                        f for f in expected_input_files(scenario) if not _input_present(ip, f)
                    ]
                    if missing_files:
                        errors.append(
                            f"Missing required input files in {ip}: {', '.join(missing_files)}"
                        )

        return ValidationResult(ok=not errors, errors=errors)

    def julia_preflight(self, config: Dict[str, Any], run_dir: str | Path) -> subprocess.CompletedProcess:
        """Run the model's own ``--preflight-only`` gate (config, input files,
        a 1-variable test solve on the chosen solver, dataset schema)."""
        run_dir = Path(run_dir)
        cfg_path = self._write_config(self.executed_config(config, run_dir), run_dir)
        return subprocess.run(
            self._command(cfg_path) + ["--preflight-only"],
            cwd=self.model_root, capture_output=True, text=True, env=self._env(),
        )

    # ---- execution -------------------------------------------------------

    def run(
        self,
        config: Dict[str, Any],
        run_dir: Path,
        on_event: Optional[Callable[..., Any]] = None,
        on_line: Optional[Callable[[str], None]] = None,
    ) -> Execution:
        """Execute one run; never raises on an infeasible/failed solve.

        ``on_line(line)`` receives every stdout/stderr line as it arrives (the
        same lines are teed into ``solver.log``). ``on_event`` is the framework's
        live-monitoring hook: an event callback (wrapped in a
        ``framework.monitor.RunMonitor`` when that module is importable, which
        also persists ``monitor.json`` in the run dir) or a ready-made monitor
        object exposing ``on_line``. Monitoring failures never fail the run.
        """
        run_dir = Path(run_dir)
        run_dir.mkdir(parents=True, exist_ok=True)
        executed = self.executed_config(config, run_dir)
        cfg_path = self._write_config(executed, run_dir)
        log_path = run_dir / "solver.log"

        monitor = self._make_monitor(executed, run_dir, on_event)
        lines: List[str] = []

        def sink(line: str) -> None:
            lines.append(line)
            if on_line is not None:
                on_line(line)
            if monitor is not None:
                try:
                    monitor.on_line(line)
                except Exception:  # monitoring must never break the run
                    pass

        start_wall = time.time()
        result = self._stream(self._command(cfg_path), log_path, sink, monitor)
        returncode = result.returncode
        if returncode is not None:
            returncode = int(returncode)
        wall = float(result.wall_seconds)
        text = "".join(lines)
        status, origin = self._parse_status(text, returncode)

        # Archive the model's CSV outputs into the run dir for an immutable
        # record (the model writes them into its own results/ tree). Only files
        # written by *this* run are taken, so a stale folder with the same
        # run_tag cannot masquerade as fresh output.
        self._archive_outputs(executed, run_dir, since=start_wall)

        execution = Execution(
            termination_status=status,
            wall_seconds=round(wall, 1),
            mipgap_reached=self._parse_gap(text),
            solver_log="solver.log",
            returncode=returncode,
            error_origin=origin,
        )
        if monitor is not None:
            summary = self._finish_monitor(monitor, result)
            if summary is not None and _EXECUTION_HAS_MONITOR:
                execution.monitor = summary  # type: ignore[attr-defined]
        return execution

    def intervention_spec(self) -> InterventionSpec:
        return InterventionSpec(
            # Tier A — numerics/labels that change the search, not the problem.
            tier_a_keys=set(TIER_A_KEYS),
            # Tier B — sanctioned parameters / scenario choice within the enum.
            tier_b_keys=set(TIER_B_KEYS),
            # Tier C — policy constraints; relaxing them changes the study.
            tier_c_keys=set(TIER_C_KEYS),
            allowed_values={k: list(v) for k, v in ALLOWED_VALUES.items()},
            # Value-level: Tier B keys whose *relaxing* transition is policy-grade.
            transition_rules=list(TRANSITION_RULES),
        )

    def locate_outputs(self, run_dir: Path) -> Dict[str, Path]:
        out_dir = Path(run_dir) / "outputs"
        if not out_dir.is_dir():
            return {}
        return {p.stem: p for p in sorted(out_dir.glob("*.csv"))}

    def describe_config(self) -> str:
        return describe_schema()

    # ---- helpers ---------------------------------------------------------

    @staticmethod
    def executed_config(config: Dict[str, Any], run_dir: str | Path) -> Dict[str, Any]:
        """The config actually handed to the model: the caller's config plus an
        injected ``run_tag`` (the run dir's basename, sanitised) when it has
        none, so concurrent runs never share a results folder. The framework's
        ``record.config`` keeps the caller's config untouched."""
        executed = dict(config)
        if not str(executed.get("run_tag", "") or "").strip():
            executed["run_tag"] = sanitise_run_tag(Path(run_dir).name)
        return executed

    @staticmethod
    def _make_monitor(config: Dict[str, Any], run_dir: Path, on_event: Any) -> Any:
        """Build the framework's live ``RunMonitor`` around an ``on_event``
        callback when ``framework.monitor`` (Workstream D) is importable.
        ``on_event`` may also already *be* a monitor (anything with an
        ``on_line`` method). Returns None when no monitoring is requested or
        available — a monitor failure must never fail the run."""
        if on_event is None:
            return None
        if callable(getattr(on_event, "on_line", None)):
            return on_event
        try:
            from framework.monitor import RunMonitor  # type: ignore
        except ImportError:
            return None
        try:
            return RunMonitor(run_dir, on_event=on_event, time_limit=config.get("time_limit"))
        except Exception:
            return None

    @staticmethod
    def _finish_monitor(monitor: Any, result: Any) -> Optional[Dict[str, Any]]:
        """Close the monitor with the stream result (so ``timed_out`` /
        ``aborted`` / ``abort_reason`` are recorded); its summary dict, or None
        when the monitor has no ``finish`` or raises — monitoring must never
        fail a run."""
        finish = getattr(monitor, "finish", None)
        if finish is None:
            return None
        try:
            summary = finish(result)
        except Exception:
            return None
        return summary if isinstance(summary, dict) else None

    def _command(self, cfg_path: Path) -> List[str]:
        return [self.julia, f"--project={self.model_root}", "run_model.jl",
                "--config", str(cfg_path)]

    def _env(self) -> Dict[str, str]:
        env = dict(os.environ)
        env["GARUDA_PYTHON"] = self.python_for_validator
        if self.skip_schema_validation:
            env["GARUDA_SKIP_VALIDATION"] = "1"
        return env

    def _write_config(self, config: Dict[str, Any], run_dir: Path) -> Path:
        run_dir = Path(run_dir)
        run_dir.mkdir(parents=True, exist_ok=True)
        cfg_path = run_dir / "config.json"
        cfg_path.write_text(json.dumps(config, indent=2))
        return cfg_path

    def _stream(self, cmd: Sequence[str], log_path: Path,
                on_line: Callable[[str], None], monitor: Any = None) -> Any:
        """Run ``cmd`` in the model root, tee its merged stdout/stderr into
        ``log_path`` line by line, and return the stream result (an object with
        ``returncode`` / ``wall_seconds``, and — on the framework path —
        ``timed_out`` / ``aborted`` / ``abort_reason``).

        With a ``monitor``, the framework's streamer also drives
        ``monitor.tick()`` about once a second so wall-clock stalls and
        time-limit-near are detected while the solver is quiet, and honours
        ``monitor.abort_event`` so ``RunMonitor.request_abort()`` stops the
        solve from inside this process. Out-of-process aborts
        (``python -m framework.watch <run_dir> --abort``) work either way: the
        ABORT file is polled regardless.
        """
        if stream_command is not None:
            kwargs: Dict[str, Any] = {}
            if monitor is not None:
                tick = getattr(monitor, "tick", None)
                if callable(tick):
                    kwargs["on_tick"] = tick
                abort = getattr(monitor, "abort_event", None)
                if abort is not None:
                    kwargs["abort"] = abort
            return stream_command(list(cmd), cwd=str(self.model_root), log_path=str(log_path),
                                  env=self._env(), on_line=on_line, **kwargs)
        # Fallback for a checkout without framework.process: same tee, no ticks.
        returncode, wall = stream_local(cmd, cwd=self.model_root, log_path=log_path,
                                        env=self._env(), on_line=on_line)
        return SimpleNamespace(returncode=returncode, wall_seconds=wall,
                               timed_out=False, aborted=False, abort_reason=None)

    def _archive_outputs(self, config: Dict[str, Any], run_dir: Path,
                         since: Optional[float] = None) -> List[Path]:
        """Copy the model's result CSVs for ``config`` into ``<run_dir>/outputs``.
        With ``since`` (epoch seconds) only files modified at/after it are taken."""
        src = self.model_results_dir(config)
        copied: List[Path] = []
        if not src.is_dir():
            return copied
        dst = Path(run_dir) / "outputs"
        for csv in sorted(src.glob("*.csv")):
            if since is not None and csv.stat().st_mtime < since - 1.0:
                continue
            dst.mkdir(parents=True, exist_ok=True)
            target = dst / csv.name
            shutil.copy2(csv, target)
            copied.append(target)
        return copied

    @staticmethod
    def _parse_status(output: str, returncode: Optional[int]) -> Tuple[str, Optional[str]]:
        """Map the engine/preflight text (+ exit code) to a canonical status."""
        failed = returncode not in (0, None)

        for marker, status, origin in _STATUS_MARKERS:
            if marker in output:
                if status == "OPTIMAL" and failed:
                    # The solve finished but the process then crashed (e.g. in
                    # result extraction): the outputs cannot be trusted.
                    return "ERROR", "runtime"
                if status == "TIME_LIMIT" and failed:
                    # Hit the limit with no incumbent to extract; still a
                    # time-limit outcome (fix = Tier A), but outputs are missing.
                    return "TIME_LIMIT", "runtime"
                if status == "ERROR":
                    m = _TERMINATION_STATUS.search(output)
                    term = (m.group(1) if m else "").upper()
                    if "INFEASIBLE" in term or "UNBOUNDED" in term:
                        # Gurobi's default INFEASIBLE_OR_UNBOUNDED (no
                        # DualReductions=0) lands on the "did not solve" line
                        # rather than the engine's infeasible line.
                        return "INFEASIBLE", "solver"
                return status, origin

        if _PREFLIGHT_OK_MARKER in output:
            return "PREFLIGHT_OK", None

        for token in _PREFLIGHT_ERROR_TOKENS:
            if token in output:
                return "ERROR", "preflight"

        # Solver-native wording without the engine marker. Gurobi reports
        # INFEASIBLE_OR_UNBOUNDED by default (no DualReductions=0), so treat any
        # infeasible/unbounded token as solver-level infeasibility even though
        # the model then crashes extracting a nonexistent objective value.
        lowered = output.lower()
        if "infeasible" in lowered or "unbounded" in lowered:
            return "INFEASIBLE", "solver"
        if "did not solve" in lowered:
            return "ERROR", "solver"

        if failed:
            origin = "preflight" if "preflight" in lowered else "runtime"
            return "ERROR", origin
        return "UNKNOWN", None

    @staticmethod
    def _parse_gap(output: str) -> Optional[float]:
        """Achieved relative MIP gap (fraction) from a Gurobi or HiGHS log, if any."""
        m = None
        for m in _GUROBI_GAP.finditer(output):
            pass
        if m is None:
            for m in _HIGHS_GAP.finditer(output):
                pass
        if m is None:
            return None
        try:
            return float(m.group(1)) / 100.0
        except ValueError:
            return None


# ---- module-level helpers (shared with the remote adapter / tests) ----------

def sanitise_run_tag(name: str) -> str:
    """A results-folder-safe tag: ``[A-Za-z0-9_.-]`` only, never empty."""
    tag = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(name)).strip("_.")
    return tag or "run"


def expected_input_files(scenario: str) -> List[str]:
    """preflight.jl::expected_input_files for the scenario's flags."""
    flags = SCENARIO_FLAGS[scenario]
    required = ["generators.csv", "demand.csv", "generators_variability.csv", "fuels_data.csv"]
    if flags["Grid"]:
        required.append("network.csv")
    if flags["VillageBuild"]:
        required += ["site_generators.csv", "site_demand.csv", "site_demandheat.csv",
                     "site_generators_variability.csv"]
    return required


def _input_present(inputs_path: Path, filename: str) -> bool:
    if (inputs_path / filename).is_file():
        return True
    for p in _SITE_PREFIXES:
        if filename.startswith(p):
            base = filename[len(p):]
            return any((inputs_path / (q + base)).is_file() for q in _SITE_PREFIXES)
    return False


def _check_value(spec: KeySpec, value: Any) -> List[str]:
    k = spec.name
    if spec.kind == "bool":
        if not isinstance(value, bool):
            return [f"{k} must be a boolean, got {value!r}"]
        return []
    if spec.kind == "str":
        if not isinstance(value, str):
            return [f"{k} must be a string, got {value!r}"]
        return []
    if spec.kind == "enum":
        if spec.enum is not None and (isinstance(value, bool) or value not in spec.enum):
            return [f"Unknown {k} {value!r}; legal values: "
                    f"{', '.join(str(v) for v in spec.enum)}"]
        return []
    # numeric kinds (bool is an int subclass in Python — reject it explicitly)
    if isinstance(value, bool) or not isinstance(value, _NUMBER):
        return [f"{k} must be a number, got {value!r}"]
    if spec.kind == "int" and int(value) != value:
        return [f"{k} must be an integer, got {value!r}"]
    if spec.enum is not None and value not in spec.enum:
        return [f"Unknown {k} {value!r}; legal values: "
                f"{', '.join(str(v) for v in spec.enum)}"]
    if spec.minimum is not None:
        if spec.exclusive_minimum and not value > spec.minimum:
            return [f"{k} must be > {spec.minimum:g}, got {value!r}"]
        if not spec.exclusive_minimum and value < spec.minimum:
            return [f"{k} must be >= {spec.minimum:g}, got {value!r}"]
    return []


def stream_local(
    cmd: Sequence[str],
    cwd: str | Path,
    log_path: str | Path,
    env: Optional[Dict[str, str]] = None,
    on_line: Optional[Callable[[str], None]] = None,
) -> Tuple[int, float]:
    """Popen + tee: merged stdout/stderr written to ``log_path`` as each line
    arrives (flushed per line) and handed to ``on_line``. Returns
    (returncode, wall_seconds). Local stand-in for
    ``framework.process.stream_command`` (Workstream D)."""
    log_path = Path(log_path)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    start = time.monotonic()
    with log_path.open("w", encoding="utf-8") as log, subprocess.Popen(
        list(cmd), cwd=str(cwd), env=env, stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT, text=True, bufsize=1, encoding="utf-8",
        errors="replace",
    ) as proc:
        assert proc.stdout is not None
        for line in proc.stdout:
            log.write(line)
            log.flush()
            if on_line is not None:
                on_line(line)
        returncode = proc.wait()
    return returncode, time.monotonic() - start


def read_headline(outputs: Dict[str, Path], metric: str) -> Optional[float]:
    """One regression headline from archived outputs (a ``locate_outputs``
    map): the first row of the column, except Total_NSE_MWh which sums the
    column over zones — exactly what the model's
    ``tests/check_dispatch_headlines.jl`` does. None when the CSV, column or a
    numeric value is missing."""
    stem, column, _ = REGRESSION_HEADLINES[metric]
    path = outputs.get(stem)
    if path is None or not Path(path).is_file():
        return None
    with Path(path).open(newline="") as f:
        rows = list(csv.DictReader(f))
    values: List[float] = []
    for row in rows:
        cell = row.get(column)
        if cell in (None, ""):
            continue
        try:
            values.append(float(cell))
        except ValueError:
            return None
    if not values:
        return None
    return sum(values) if metric == "Total_NSE_MWh" else values[0]


def check_regression_headlines(
    outputs: Dict[str, Path], tol: float = REGRESSION_TOLERANCE,
) -> Dict[str, Tuple[Optional[float], float, bool]]:
    """``{metric: (got, expected, within_tol)}`` for the regression case's
    three headline numbers (relative tolerance ``tol``, default +-1 %)."""
    report: Dict[str, Tuple[Optional[float], float, bool]] = {}
    for metric, (_, _, expected) in REGRESSION_HEADLINES.items():
        got = read_headline(outputs, metric)
        ok = got is not None and abs(got - expected) <= tol * abs(expected)
        report[metric] = (got, expected, ok)
    return report


def describe_schema() -> str:
    """The full human-readable schema — what an LLM needs to build or refine a
    config for this model without reading the Julia."""
    lines: List[str] = []
    a = lines.append
    a("garuda — zonal capacity-expansion / dispatch platform for Indonesian islands "
      "(Julia/JuMP; solver HiGHS or Gurobi). Case study adapter 'garuda'.")
    a("")
    a("CONFIG = one flat JSON object. Inputs are read from "
      "data_indonesia/<year>/<island>/ inside the model; results are written to "
      "results/<scenario>_<island>_<year>_<clean>[__<run_tag>]/ and archived by the "
      "adapter into <run_dir>/outputs/*.csv.")
    a("")
    a(f"REQUIRED keys: {', '.join(REQUIRED_KEYS)}.")
    a("  island (str): dataset folder, e.g. maluku, timor_demo, timor_belu, sumatera, "
      "jawa_bali, kalimantan, sulawesi, papua, nusa_tenggara, north_maluku, timor "
      "(2030); the first 8 also exist for 2035. Must match an existing "
      "data_indonesia/<year>/<island>/ folder.")
    a("  year (str, e.g. \"2030\"): dataset year — a string, not a number.")
    for k in ("scenario", "clean", "CO235reduction", "BAUCO2emissions", "CO2_limit"):
        spec = _KEYSPEC_BY_NAME[k]
        a(f"  {k} ({spec.kind}, tier {spec.tier}): {spec.doc}")
    a("")
    a("SCENARIO semantics (scenario_settings in preflight.jl):")
    a("  base = grid layer only, no grid expansion, no site build; grid = grid "
      "expansion (network.csv) only; village = standalone site (village/industrial-"
      "park) build, no grid; gridvillage = coordinated grid + site build; "
      "highimportprice = gridvillage with import price x1.21; nocoal = the same settings "
      "as grid (grid expansion, no site build, base import price) plus the NoCoal flag, "
      "whose ONLY modelled effect is to restrict the village/site electricity balance to "
      "renewable village units (optimizer.jl cVILElectricityBalance over inputs.VIL_RE) — "
      "it does NOT ban coal on the grid layer, where coal is still built and dispatched "
      "exactly as in 'grid'; captive/gridcaptive = legacy aliases of village/gridvillage. "
      "village-type scenarios need the site_*/village_*/ip_* input tables (maluku has "
      "none; timor_demo and timor_belu do).")
    a("")
    a("OPTIONAL keys (all read by run_model.jl; a key the model does not know is "
      "silently ignored, so only these are accepted):")
    for spec in OPTIONAL_KEYS:
        if spec.name in REQUIRED_KEYS:
            continue
        default = "engine-dependent" if spec.name == "relax_uc" else json.dumps(spec.default)
        extra = f"; legal: {spec.enum}" if spec.enum is not None else ""
        a(f"  {spec.name} ({spec.kind}, default {default}, tier {spec.tier}{extra}): {spec.doc}")
    a("")
    a("INTERVENTION TIERS (the guardrail):")
    a(f"  A auto-apply (numerics/labels): {sorted(TIER_A_KEYS)}")
    a(f"  B auto-apply + flag (sanctioned parameters): {sorted(TIER_B_KEYS)}")
    a(f"  C human sign-off, never silent (POLICY levers): {sorted(TIER_C_KEYS)}")
    a("  island/year are not in any tier: changing the dataset is a study change, "
      "so a proposal on them defaults to Tier C.")
    a("  Value-level escalations — a Tier B key whose *transition* is policy-grade and "
      "therefore stops the loop for human sign-off:")
    for rule in TRANSITION_RULES:
        a(f"    {rule.key}: {rule.from_values} -> {rule.to_values} is Tier "
          f"{rule.tier.value} — {rule.reason}. The reverse direction stays Tier B.")
    a("")
    a("ENGINE / SOLVER CHOICE AND COST: dispatch = LP on HiGHS, seconds to a few "
      "minutes on any island; expansion = MILP with exact unit commitment — ~25 min on "
      "HiGHS for timor_demo, ~5 s on Gurobi (licence); relax_uc=true makes expansion a "
      "fast LP approximation. Larger islands (maluku, sumatera, jawa_bali) expansion "
      "MILPs take hours on Gurobi. Use time_limit on MILPs rather than killing the "
      "process. There is no direct 'X % solar' knob — penetration is an outcome of "
      "costs/resources, not a config key.")
    a("")
    a("STATUS LINES the engine prints: 'Capacity expansion|Dispatch solved successfully "
      "(...)' -> OPTIMAL; '... reached the time limit' -> TIME_LIMIT (incumbent still "
      "extracted); 'Capacity expansion is infeasible.' / 'Dispatch is infeasible (...)' -> "
      "INFEASIBLE (the process then crashes on objective_value, so exit code != 0 is "
      "normal); '... did not solve. Termination status: X' -> ERROR(solver); preflight "
      "errors ('Input data directory not found', 'missing required keys', 'Unknown "
      "scenario', 'Unknown clean flag', 'Input schema validation failed', 'could not start "
      "a working optimizer') -> ERROR(preflight). 'WARNING: export_price (...) > "
      "import_price (...)' flags a configured arbitrage (an output-anomaly signal) on a "
      "dataset that has grid demand; on a dataset with NO grid demand the model instead "
      "aborts with 'export_price (...) > import_price (...) on a dataset with NO grid "
      "demand' (unless export_backed_by_generation is true) -> ERROR(preflight).")
    a("")
    a(f"OUTPUT CSVs (which appear depends on scenario/engine): {', '.join(RESULT_CSVS)}. "
      "Headline metrics: cost_results.Total_Costs ($M/yr), clean_energy_results."
      "CO2_Emissions (tCO2/yr) and Grid_REShare, reliability_results.Total_NSE_MWh "
      "(dispatch only; sum over zones), cost_results.Village_Export_Revenue.")
    a("")
    a("REGRESSION CASE (the model's CI; HiGHS, no licence, minutes): "
      f"{json.dumps(REGRESSION_CONFIG)} -> within +-1 %: "
      + ", ".join(f"{m} = {v[2]}" for m, v in REGRESSION_HEADLINES.items())
      + ". The 12.4 % unserved energy on maluku dispatch is a real reliability gap in "
      "the dataset, not an anomaly.")
    return "\n".join(lines)
