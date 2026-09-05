"""Pure, stdlib-only helpers for the pathways adapter — the "config-injection shim".

The China RE Pathways model (``models/pathways``) has no config-file entry point:
its test scripts mutate a ``scen_params`` dict in Python and every read/write is
relative to a ``work_dir`` derived from where ``callUtility.py`` lives. This
module holds everything needed to drive it *without editing it*:

- the flat, adapter-defined config schema (keys, enums, defaults, tiers);
- ``build_scen_params``: config -> the model's ``scen_params`` dict
  (mirrors ``pycode/testMultiYear.py`` lines 101-148 for the cost trajectories);
- ``build_workspace``: a per-run directory of symlinks that relocates the
  model's ``work_dir`` so inputs are shared read-only and outputs land in the
  run directory (with one CSV materialised for the emission-cap override);
- Gurobi parameter-file rendering (``gurobi.env``) and Gurobi log status parsing;
- driver-output parsing and output archiving helpers.

Nothing here imports pandas, gurobipy, or the model, so it is testable on any
Python and is loaded by both the framework-side adapter and the model-side
``driver.py`` (which runs inside the model's conda environment).
"""

from __future__ import annotations

import copy
import os
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# ---------------------------------------------------------------------------
# Config schema
# ---------------------------------------------------------------------------

#: Modelled years with a complete input set (coal_natural_retire/coal_<year>_pre.csv
#: exists only for these; MultiYearAutomation projects 2025..2060 in 5-year steps).
YEARS: List[int] = [2030, 2040, 2050, 2060]
#: ``MultiYearAutomation(yr_start=2025, yr_end=2060, yr_step=5).yr_req``
YEAR_SEQUENCE: List[int] = list(range(2025, 2061, 5))
EMISSION_TARGETS = ["2C", "15C"]
CCS_START_YEARS = [2040, 2050, 2060, 2070]
HEATING_MODES = ["chp_ccs", "heat_pump"]
COST_DECLINES = ["baseline", "conservative"]
DEMAND_SENSITIVITIES = ["none", "p5", "m5"]
#: Only the 2015 weather year ships with the Zenodo data (initData.cell_file/mat_file).
VRE_YEARS = ["w2015_s2015"]
BINARY = [0, 1]
NUMERIC_FOCUS = [0, 1, 2, 3]

#: Defaults for every optional key. The scenario/cost defaults equal the model's
#: ``data_csv/scen_params_template.json`` so that a config carrying only ``year``
#: reproduces the template (for 2060; other years follow the cost trajectory).
DEFAULTS: Dict[str, Any] = {
    "optimization_days": 5,
    "optimization_step": 10,
    "vre_year": "w2015_s2015",
    "emission_target": "2C",
    "comply_with_medium_vre_goal": 0,
    "ccs_start_year": 2040,
    "heating_electrification": "chp_ccs",
    "renewable_cost_decline": "baseline",
    "endogenize_firm_capacity": 1,
    "demand_sensitivity": "none",
    "demand_scale": 1.0,
    "ccs_retrofit_cost": 3500,
    "wacc": 7.4,
    "with_shedding": 0,
    "demand_resv": 0.05,
    "vre_resv": 0.05,
    "wind_with_xz": 0,
    "res_tag": "agentic",
}

#: Keys that are optional AND have no default (absent = not applied).
OPTIONAL_NO_DEFAULT = ["emission_cap_override_mt", "time_limit", "threads",
                       "bar_conv_tol", "numeric_focus"]

REQUIRED_KEYS = ["year"]

#: The smallest configuration that still exercises the whole flow: 2060 (the
#: year with the most binding emission target), 3 sampled days, default policy.
#: Used by ``examples/pathways/smoke_test.py`` and the ``baseline_2060_short``
#: fixture. NOT run in this build — Pathways needs Gurobi + the Zenodo data.
SMOKE_CONFIG: Dict[str, Any] = {
    "year": 2060,
    "optimization_days": 3,
    "optimization_step": 30,
    "emission_target": "2C",
    "ccs_start_year": 2040,
    "res_tag": "smoke",
}

TIER_A_KEYS = {"time_limit", "threads", "bar_conv_tol", "numeric_focus", "res_tag"}
TIER_B_KEYS = {
    "renewable_cost_decline", "demand_sensitivity", "demand_scale",
    "heating_electrification", "endogenize_firm_capacity", "ccs_retrofit_cost",
    "wacc", "optimization_days", "optimization_step", "year", "vre_year",
}
TIER_C_KEYS = {
    "emission_target", "comply_with_medium_vre_goal", "ccs_start_year",
    "with_shedding", "demand_resv", "vre_resv", "wind_with_xz",
    "emission_cap_override_mt",
}
ALL_KEYS = TIER_A_KEYS | TIER_B_KEYS | TIER_C_KEYS
BINARY_KEYS = {"comply_with_medium_vre_goal", "endogenize_firm_capacity",
               "with_shedding", "wind_with_xz"}

ALLOWED_VALUES: Dict[str, List[Any]] = {
    "year": YEARS,
    "emission_target": EMISSION_TARGETS,
    "ccs_start_year": CCS_START_YEARS,
    "heating_electrification": HEATING_MODES,
    "renewable_cost_decline": COST_DECLINES,
    "endogenize_firm_capacity": BINARY,
    "demand_sensitivity": DEMAND_SENSITIVITIES,
    "comply_with_medium_vre_goal": BINARY,
    "with_shedding": BINARY,
    "wind_with_xz": BINARY,
    "numeric_focus": NUMERIC_FOCUS,
    "vre_year": VRE_YEARS,
}

#: Config key -> Gurobi parameter written to ``gurobi.env``. ``Method`` and
#: ``Crossover`` are set by the model in code (main.py:347-348) and would be
#: overridden, so they are deliberately not exposed.
GUROBI_PARAMS = {
    "time_limit": "TimeLimit",
    "threads": "Threads",
    "bar_conv_tol": "BarConvTol",
    "numeric_focus": "NumericFocus",
}

_RES_TAG_RE = re.compile(r"[^A-Za-z0-9_\-]+")


def sanitize_tag(tag: Any) -> str:
    """Result-folder label: letters, digits, ``_`` and ``-`` only."""
    cleaned = _RES_TAG_RE.sub("_", str(tag)).strip("_")
    return cleaned or "agentic"


def effective_config(config: Dict[str, Any]) -> Dict[str, Any]:
    """Defaults merged under the user's config; bools coerced to 0/1 for the
    binary keys; ``res_tag`` sanitised. Does not validate."""
    cfg: Dict[str, Any] = {**DEFAULTS, **config}
    for key in BINARY_KEYS:
        if isinstance(cfg.get(key), bool):
            cfg[key] = int(cfg[key])
    cfg["res_tag"] = sanitize_tag(cfg.get("res_tag", DEFAULTS["res_tag"]))
    return cfg


def _is_int(v: Any) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def _is_number(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def validate_schema(config: Dict[str, Any]) -> List[str]:
    """Filesystem-free checks: required keys, unknown keys, enums, types, ranges.
    Shared by the local adapter, the remote adapter, and the driver."""
    errors: List[str] = []
    if not isinstance(config, dict):
        return ["config must be a JSON object"]

    missing = [k for k in REQUIRED_KEYS if k not in config]
    if missing:
        errors.append(f"Missing required config keys: {', '.join(missing)}")

    unknown = sorted(k for k in config if k not in ALL_KEYS)
    if unknown:
        errors.append(
            f"Unknown config keys: {', '.join(unknown)} (legal keys: {', '.join(sorted(ALL_KEYS))})"
        )

    cfg = effective_config({k: v for k, v in config.items() if k in ALL_KEYS})

    # enums
    for key, allowed in ALLOWED_VALUES.items():
        if key in config and cfg[key] not in allowed:
            errors.append(f"Illegal value {config[key]!r} for {key!r}; legal values: {allowed}")

    # integer-typed keys
    for key in ("year", "ccs_start_year", "optimization_days", "optimization_step", "threads",
                "numeric_focus"):
        if key in config and not _is_int(config[key]):
            errors.append(f"{key!r} must be an integer, got {config[key]!r}")

    # positive numerics
    for key in ("optimization_days", "optimization_step", "threads"):
        if key in config and _is_int(config[key]) and config[key] < 1:
            errors.append(f"{key!r} must be >= 1, got {config[key]!r}")
    for key in ("demand_scale", "time_limit", "bar_conv_tol"):
        if key in config:
            v = config[key]
            if not _is_number(v) or v <= 0:
                errors.append(f"{key!r} must be a number > 0, got {v!r}")
    for key in ("ccs_retrofit_cost", "wacc", "demand_resv", "vre_resv"):
        if key in config:
            v = config[key]
            if not _is_number(v) or v < 0:
                errors.append(f"{key!r} must be a number >= 0, got {v!r}")
    if "emission_cap_override_mt" in config and not _is_number(config["emission_cap_override_mt"]):
        errors.append("'emission_cap_override_mt' must be a number (Mt CO2 for the modelled year)")

    # horizon must fit in one 8760-h year (initData.seedHour silently drops hours past 8759)
    days, step = cfg["optimization_days"], cfg["optimization_step"]
    if _is_int(days) and _is_int(step) and (days - 1) * step > 364:
        errors.append(
            f"optimization_days={days} x optimization_step={step} exceeds one year "
            f"((days-1)*step must be <= 364); seedHour would silently drop hours"
        )

    if "res_tag" in config and not isinstance(config["res_tag"], str):
        errors.append("'res_tag' must be a string")
    if "vre_year" in config and not re.fullmatch(r"w\d{4}_s\d{4}", str(config["vre_year"])):
        errors.append(f"'vre_year' must look like w2015_s2015, got {config['vre_year']!r}")

    return errors


# ---------------------------------------------------------------------------
# config -> scen_params (mirrors testMultiYear.py lines 101-148)
# ---------------------------------------------------------------------------

def _linspace(start: float, stop: float, n: int) -> List[float]:
    """``numpy.linspace`` for the cost trajectories, without numpy."""
    if n == 1:
        return [float(start)]
    step = (stop - start) / (n - 1)
    return [start + step * i for i in range(n)]


# Conservative cost declines based on NREL projections (2023) — testMultiYear.py
_CONSERVATIVE = {
    "on_wind": [7700, 7700, 7197.2, 7197.2, 6711.4, 6711.4, 6225.5, 6225.5, 5739.7],
    "off_wind": [15000, 15000, 13008.3, 13008.3, 12256.5, 12256.5, 11799.3, 11799.3, 11342.0],
    "pv": [5300, 5300, 4951.0, 4951.0, 4150.5, 4150.5, 3403.3, 3403.3, 2656.2],
    "dpv": [5300, 5300, 5112.7, 5112.7, 4296.8, 4296.8, 3468.9, 3468.9, 2640.9],
    "bat": [6000, 6000, 5644.6, 5644.6, 5259.0, 5259.0, 4869.6, 4869.6, 4480.2],
}


def cost_trajectory(year: int, decline: str) -> Dict[str, float]:
    """Per-decade capex/O&M values exactly as testMultiYear.py computes them for
    the year at index ``idx`` of the 2025..2060 sequence (it reads ``list[idx+1]``
    of an ``len(yr_req)+1``-point trajectory)."""
    n = len(YEAR_SEQUENCE) + 1
    idx = YEAR_SEQUENCE.index(int(year))
    i = idx + 1
    if decline == "baseline":
        on_wind = _linspace(7700, 3000, n)
        off_wind = _linspace(15000, 5400, n)
        pv = _linspace(5300, 1500, n)
        dpv = _linspace(5300, 2000, n)
        bat = _linspace(6000, 2700, n)
        caes = _linspace(24000, 4800, n)
        vrb = _linspace(25600, 3000, n)
    elif decline == "conservative":
        on_wind = _CONSERVATIVE["on_wind"]
        off_wind = _CONSERVATIVE["off_wind"]
        pv = _CONSERVATIVE["pv"]
        dpv = _CONSERVATIVE["dpv"]
        bat = _CONSERVATIVE["bat"]
        caes = _linspace(24000, 12000, n)
        vrb = _linspace(25600, 12500, n)
    else:
        raise ValueError(f"unknown renewable_cost_decline {decline!r}")
    return {
        "capex_equip_on_wind": on_wind[i] - 800,
        "capex_om_on_wind": _linspace(170, 45, n)[i],
        "capex_equip_off_wind": off_wind[i] - 1600,
        "capex_om_off_wind": _linspace(715, 81, n)[i],
        "capex_equip_pv": pv[i] - 400,
        "capex_om_pv": _linspace(85, 7.5, n)[i],
        "capex_equip_dpv": dpv[i] - 600,
        "capex_om_dpv": _linspace(107, 10, n)[i],
        "capex_power_phs": _linspace(3840, 3840, n)[i],
        "capex_power_bat": bat[i],
        "capex_power_lds_caes": caes[i],
        "capex_power_lds_vrb": vrb[i],
    }


def build_scen_params(template: Dict[str, Any], config: Dict[str, Any]) -> Dict[str, Any]:
    """Apply a flat adapter config onto a copy of ``scen_params_template.json``.

    Pure: the template is deep-copied, never mutated. Every mapping below is a
    line in ``testMultiYear.py``/``testSingleYear.py`` or a documented consumer in
    ``main.py``/``initData.py``.
    """
    cfg = effective_config(config)
    sp = copy.deepcopy(template)
    year = int(cfg["year"])

    scen = sp["scenario"]
    scen["comply_with_medium_vre_goal"] = cfg["comply_with_medium_vre_goal"]   # main.py:1266
    scen["endogenize_firm_capacity"] = cfg["endogenize_firm_capacity"]         # main.py:457
    scen["ccs_start_year"] = int(cfg["ccs_start_year"])                        # main.py:477,527,1334
    scen["emission_target"] = cfg["emission_target"]                           # main.py:511,1277
    scen["heating_electrification"] = cfg["heating_electrification"]           # main.py:479
    scen["renewable_cost_decline"] = cfg["renewable_cost_decline"]
    scen["demand_sensitivity"] = cfg["demand_sensitivity"]                     # informational
    scen["emission_factor_method"] = "mean"                                    # main.py:1278

    sp["ccs"]["capex_coal_ccs"] = cfg["ccs_retrofit_cost"]                     # testMultiYear:110
    sp["ccs"]["capex_gas_ccs"] = cfg["ccs_retrofit_cost"]                      # testMultiYear:111

    traj = cost_trajectory(year, cfg["renewable_cost_decline"])
    vre = sp["vre"]
    for k in ("capex_equip_on_wind", "capex_om_on_wind", "capex_equip_off_wind",
              "capex_om_off_wind", "capex_equip_pv", "capex_om_pv",
              "capex_equip_dpv", "capex_om_dpv"):
        vre[k] = traj[k]
    sp["storage"]["capex_power_phs"] = traj["capex_power_phs"]
    sp["storage"]["capex_power_bat"] = traj["capex_power_bat"]
    sp["storage"]["capex_power_lds"]["caes"] = traj["capex_power_lds_caes"]
    sp["storage"]["capex_power_lds"]["vrb"] = traj["capex_power_lds_vrb"]

    sp["optimization_hours"]["step"] = int(cfg["optimization_step"])
    sp["optimization_hours"]["days"] = int(cfg["optimization_days"])

    sp["finance"]["weighted_average_cost_of_capital"] = cfg["wacc"]
    sp["demand"]["scale"] = cfg["demand_scale"]                                 # initData.py:949
    sp["resv"]["demand_resv"] = cfg["demand_resv"]                             # main.py:329
    sp["resv"]["vre_resv"] = cfg["vre_resv"]                                   # main.py:328
    sp["shedding"]["with_shedding"] = cfg["with_shedding"]                     # main.py:414,1010
    sp["vre"]["wind_with_xz"] = cfg["wind_with_xz"]                            # main.py:360
    return sp


# ---------------------------------------------------------------------------
# Demand scaling (materialised CSVs)
# ---------------------------------------------------------------------------

#: Folder the model's ``MultiYearAutomation.demand_projection`` writes per year,
#: under ``data_res/<res_tag>_<vre_year>/<year>/``. ``initData.initDemLayer``
#: reads every ``<Province>.csv`` in it (columns ``hour,dem``).
DEMAND_DIRNAME = "provin_demand_hourly"
DEMAND_COLUMN = "dem"


def scale_demand_csv(text: str, factor: float, column: str = DEMAND_COLUMN) -> str:
    """Multiply the ``dem`` column of one provincial hourly-demand CSV.

    Why this exists: ``initData.initDemLayer`` *reads*
    ``scen_params["demand"]["scale"]`` into ``alpha`` (initData.py:949) but the
    only block that ever used ``alpha`` is commented out (initData.py:1006-1013),
    so setting the scen_params key alone is a no-op in the model as pinned. To
    make ``demand_scale`` a real lever without editing the model, the driver
    rewrites the per-run workspace's demand CSVs instead — they live under
    ``data_res/`` inside the run directory, so nothing shared is touched.

    Pure text -> text; the header and every other column are preserved.
    """
    lines = text.splitlines()
    if not lines:
        return text
    header = [c.strip() for c in lines[0].split(",")]
    if column not in header:
        raise KeyError(f"demand CSV has no {column!r} column (header: {header})")
    col = header.index(column)
    out = [lines[0]]
    for line in lines[1:]:
        if not line.strip():
            out.append(line)
            continue
        cells = line.split(",")
        if len(cells) <= col:
            out.append(line)
            continue
        try:
            cells[col] = repr(float(cells[col]) * float(factor))
        except ValueError:
            out.append(line)
            continue
        out.append(",".join(cells))
    return "\n".join(out) + "\n"


def scale_demand_dir(demand_dir: str | os.PathLike, factor: float) -> List[str]:
    """Apply :func:`scale_demand_csv` in place to every provincial CSV in
    ``demand_dir``. Returns the file names touched (sorted). A factor of 1 is a
    no-op. Files without a ``dem`` column (e.g. the ``nation_dem_full.csv`` the
    model writes later) are skipped."""
    folder = Path(demand_dir)
    touched: List[str] = []
    if float(factor) == 1.0 or not folder.is_dir():
        return touched
    for csv_path in sorted(folder.glob("*.csv")):
        try:
            scaled = scale_demand_csv(csv_path.read_text(), factor)
        except KeyError:
            continue
        csv_path.write_text(scaled)
        touched.append(csv_path.name)
    return touched


# ---------------------------------------------------------------------------
# Emission-cap override (materialised CSV)
# ---------------------------------------------------------------------------

def emission_csv_name(emission_target: str) -> str:
    return f"power_sector_emission_{emission_target}.csv"


def override_emission_csv(text: str, year: int, value: float) -> str:
    """Return ``text`` (``Year,CO2_emission`` CSV) with the row for ``year``
    replaced by ``value``. Other rows and the header are untouched."""
    lines = text.splitlines()
    if not lines:
        raise ValueError("empty emission CSV")
    out: List[str] = []
    found = False
    for i, line in enumerate(lines):
        cells = line.split(",")
        if i > 0 and cells and cells[0].strip().isdigit() and int(cells[0]) == int(year):
            out.append(f"{int(year)},{value!r}" if isinstance(value, float) else f"{int(year)},{value}")
            found = True
        else:
            out.append(line)
    if not found:
        raise KeyError(f"year {year} not present in emission CSV")
    return "\n".join(out) + "\n"


# ---------------------------------------------------------------------------
# Gurobi parameter file
# ---------------------------------------------------------------------------

def gurobi_params(config: Dict[str, Any], log_file: Optional[str | os.PathLike] = None) -> Dict[str, Any]:
    """Gurobi parameters implied by the config (+ LogFile so the full solver log
    always exists on disk, independent of stdout buffering)."""
    params: Dict[str, Any] = {}
    for key, name in GUROBI_PARAMS.items():
        if key in config and config[key] is not None:
            params[name] = config[key]
    if log_file is not None:
        params["LogFile"] = str(log_file)
    return params


def gurobi_env_text(params: Dict[str, Any]) -> str:
    """Render a ``gurobi.env`` parameter file (``Name value`` per line). Gurobi
    reads it from the current working directory when the Env is created."""
    return "".join(f"{name} {value}\n" for name, value in params.items())


# ---------------------------------------------------------------------------
# Per-run workspace
# ---------------------------------------------------------------------------

DATA_DIRS: Tuple[str, ...] = ("data_pkl", "data_mat", "data_shp")
#: data_csv sub-directories materialised as real directories of file symlinks
#: (so a single CSV can be replaced by a real file without touching the model).
MATERIALISED_CSV_DIRS: Tuple[str, ...] = ("capacity_assumptions",)


class WorkspaceError(RuntimeError):
    """A required model or data path is missing; nothing was solved."""


@dataclass
class Workspace:
    root: Path
    pycode: Path
    data_csv: Path
    data_res: Path
    gurobi_env: Optional[Path] = None
    overrides: List[str] = field(default_factory=list)


def resolve_data_root(data_root: str | os.PathLike | None) -> Optional[Path]:
    """Directory that holds ``data_pkl/data_mat/data_shp``. Tolerates the Zenodo
    archive unpacking one level deep (``<root>/<name>/data_pkl``)."""
    if data_root is None:
        return None
    root = Path(data_root).expanduser()
    if all((root / d).is_dir() for d in DATA_DIRS):
        return root
    if root.is_dir():
        for child in sorted(p for p in root.iterdir() if p.is_dir()):
            if all((child / d).is_dir() for d in DATA_DIRS):
                return child
    return root


def check_model_root(model_root: str | os.PathLike) -> List[str]:
    root = Path(model_root)
    errors = []
    for rel in ("pycode/main.py", "pycode/callUtility.py", "data_csv/scen_params_template.json"):
        if not (root / rel).is_file():
            errors.append(f"model file not found: {root / rel}")
    return errors


def check_data_root(data_root: str | os.PathLike | None) -> List[str]:
    if data_root is None:
        return ["PATHWAYS_DATA_ROOT is not set: the Zenodo data (data_pkl/, data_mat/, "
                "data_shp/) is required for any run"]
    root = resolve_data_root(data_root)
    missing = [d for d in DATA_DIRS if not (root / d).is_dir()]
    if missing:
        return [f"data directory not found under {root}: {', '.join(missing)} "
                f"(expected the unpacked Zenodo folders)"]
    return []


def _symlink(target: Path, link: Path) -> None:
    link.symlink_to(target, target_is_directory=target.is_dir())


def build_workspace(
    root: str | os.PathLike,
    model_root: str | os.PathLike,
    data_root: str | os.PathLike | None,
    *,
    emission_override: Optional[Tuple[str, int, float]] = None,
    gurobi_params: Optional[Dict[str, Any]] = None,
    wipe: bool = True,
) -> Workspace:
    """Create ``<root>/`` so that the model, imported through ``<root>/pycode``,
    computes ``work_dir == <root>/``:

    - ``pycode -> <model_root>/pycode`` (symlink; ``callUtility.getWorkDir`` uses
      ``abspath`` without resolving symlinks, so work_dir relocates here);
    - ``data_pkl|data_mat|data_shp -> <data_root>/...`` (symlinks);
    - ``data_csv/`` real dir mirroring ``<model_root>/data_csv`` entry-by-entry
      with symlinks, except ``capacity_assumptions/`` which is a real dir of
      per-entry symlinks so one CSV can be replaced by a real file;
    - ``data_res/`` real dir (all model outputs land here);
    - ``gurobi.env`` when ``gurobi_params`` is given.

    ``emission_override=(target, year, value)`` materialises
    ``data_csv/capacity_assumptions/power_sector_emission_<target>.csv`` with the
    row for ``year`` set to ``value``.
    """
    root = Path(root)
    model_root = Path(model_root)
    errors = check_model_root(model_root)
    if data_root is not None:
        errors += check_data_root(data_root)
    if errors:
        raise WorkspaceError("; ".join(errors))

    if root.exists() and wipe:
        shutil.rmtree(root)
    root.mkdir(parents=True, exist_ok=True)

    pycode = root / "pycode"
    _symlink(model_root / "pycode", pycode)

    if data_root is not None:
        resolved = resolve_data_root(data_root)
        for d in DATA_DIRS:
            _symlink(resolved / d, root / d)

    data_csv = root / "data_csv"
    data_csv.mkdir()
    for entry in sorted((model_root / "data_csv").iterdir()):
        if entry.name in MATERIALISED_CSV_DIRS and entry.is_dir():
            sub = data_csv / entry.name
            sub.mkdir()
            for child in sorted(entry.iterdir()):
                _symlink(child, sub / child.name)
        else:
            _symlink(entry, data_csv / entry.name)

    data_res = root / "data_res"
    data_res.mkdir()

    ws = Workspace(root=root, pycode=pycode, data_csv=data_csv, data_res=data_res)

    if emission_override is not None:
        target, year, value = emission_override
        link = data_csv / "capacity_assumptions" / emission_csv_name(target)
        if not link.exists():
            raise WorkspaceError(f"emission target CSV not found: {link}")
        original = link.read_text()
        link.unlink()
        link.write_text(override_emission_csv(original, int(year), value))
        ws.overrides.append(f"{link.relative_to(root)}: {year} -> {value}")

    if gurobi_params:
        ws.gurobi_env = root / "gurobi.env"
        ws.gurobi_env.write_text(gurobi_env_text(gurobi_params))

    return ws


def results_relpath(config: Dict[str, Any]) -> str:
    """Where the model writes this config's outputs, relative to the workspace:
    ``data_res/<res_tag>_<vre_year>/<year>``."""
    cfg = effective_config(config)
    return os.path.join("data_res", f"{cfg['res_tag']}_{cfg['vre_year']}", str(int(cfg["year"])))


# ---------------------------------------------------------------------------
# Status parsing
# ---------------------------------------------------------------------------

#: (regex on a stripped line, canonical status). Scanned line by line; the LAST
#: matching line wins, which copes with barrier -> fallback sequences.
GUROBI_STATUS_PATTERNS: List[Tuple[re.Pattern, str]] = [
    (re.compile(r"^Optimal objective\b"), "OPTIMAL"),
    (re.compile(r"^Optimal solution found\b"), "OPTIMAL"),
    (re.compile(r"^Barrier solved model\b"), "OPTIMAL"),
    (re.compile(r"^Solved in \d+ iterations\b"), "OPTIMAL"),
    (re.compile(r"^Model is infeasible or unbounded\b"), "INFEASIBLE"),
    (re.compile(r"^Infeasible or unbounded model\b"), "INFEASIBLE"),
    (re.compile(r"^Model is infeasible\b"), "INFEASIBLE"),
    (re.compile(r"^Infeasible model\b"), "INFEASIBLE"),
    (re.compile(r"^Model is unbounded\b"), "ERROR"),
    (re.compile(r"^Unbounded model\b"), "ERROR"),
    (re.compile(r"^Time limit reached\b"), "TIME_LIMIT"),
    (re.compile(r"^Work limit reached\b"), "TIME_LIMIT"),
    (re.compile(r"Numerical trouble encountered\b"), "ERROR"),
    (re.compile(r"^Numeric error\b"), "ERROR"),
    (re.compile(r"^Sub-optimal termination\b"), "ERROR"),
    (re.compile(r"^Solve interrupted\b"), "ERROR"),
    (re.compile(r"^Out of memory\b"), "ERROR"),
    # Deliberately NOT matched here: gurobipy's ``GurobiError`` traceback text.
    # ``parse_driver_output`` falls back to scanning the driver's whole stdout
    # when ``gurobi.log`` is missing, and the traceback that *follows* an
    # infeasible solve (main.py reads ``.objVal`` without a status check) would
    # then win under last-match-wins and mask the real INFEASIBLE.
]


def parse_gurobi_status(text: str) -> Tuple[Optional[str], Optional[str]]:
    """Canonical status from Gurobi log text: ``(status, matched_line)`` or
    ``(None, None)`` when no termination marker is present."""
    status: Optional[str] = None
    matched: Optional[str] = None
    for raw in text.splitlines():
        line = raw.strip()
        for pattern, st in GUROBI_STATUS_PATTERNS:
            if pattern.search(line):
                status, matched = st, line
                break
    return status, matched


MARKER_PREFIX = "[pathways]"
_MARKER_RE = re.compile(r"^\[pathways\] (?P<kind>[a-z_]+): ?(?P<msg>.*)$")
STAGES = ("config", "workspace", "import", "inputs", "solve", "postprocess", "done")
TERMINAL_STATUSES = ("OPTIMAL", "INFEASIBLE", "TIME_LIMIT", "ERROR")


@dataclass
class DriverOutcome:
    status: str
    error_origin: Optional[str]
    stage: Optional[str]
    reason: Optional[str]
    gurobi_line: Optional[str] = None
    warnings: List[str] = field(default_factory=list)


def parse_markers(text: str) -> Dict[str, Any]:
    """Collect ``[pathways] <kind>: <msg>`` marker lines from driver output."""
    found: Dict[str, Any] = {"stages": [], "warnings": []}
    for raw in text.splitlines():
        m = _MARKER_RE.match(raw.strip())
        if not m:
            continue
        kind, msg = m.group("kind"), m.group("msg").strip()
        if kind == "stage":
            found["stages"].append(msg)
        elif kind == "warning":
            found["warnings"].append(msg)
        else:
            found[kind] = msg
    return found


def origin_for(status: str, stage: Optional[str], gurobi_status: Optional[str]) -> Optional[str]:
    """error_origin ∈ {preflight, solver, runtime, None} from status + last stage."""
    if status == "OPTIMAL":
        return None
    if status in ("INFEASIBLE", "TIME_LIMIT"):
        return "solver"
    if stage in (None, "config", "workspace", "import", "inputs"):
        return "preflight"
    if stage == "solve":
        return "solver" if gurobi_status is not None else "runtime"
    return "runtime"


def parse_driver_output(text: str, returncode: Optional[int], gurobi_log: str = "",
                        driver_result: Optional[Dict[str, Any]] = None) -> DriverOutcome:
    """Status/origin from the driver's ``driver_result.json`` when it exists,
    then its marker lines, then the Gurobi log (streamed stdout and/or the
    ``LogFile``), and finally the return code.

    ``driver_result`` is authoritative when it carries a terminal status: the
    driver writes it last, from inside the model's environment, where it can see
    both the Gurobi log and any exception the model raised.
    """
    markers = parse_markers(text)
    stage = markers["stages"][-1] if markers["stages"] else None
    g_status, g_line = parse_gurobi_status(gurobi_log or "")
    if g_status is None:
        g_status, g_line = parse_gurobi_status(text)

    status = markers.get("status")
    reason = markers.get("reason")
    if driver_result and driver_result.get("status") in TERMINAL_STATUSES:
        status = driver_result["status"]
        reason = driver_result.get("reason") or reason
        stage = driver_result.get("stage") or stage
        g_line = driver_result.get("gurobi_line") or g_line
    if status not in TERMINAL_STATUSES:
        if g_status is not None:
            status = g_status
            reason = reason or f"derived from Gurobi log: {g_line}"
        elif returncode not in (0, None):
            status = "ERROR"
            reason = reason or f"driver exited with code {returncode} without a status marker"
        elif returncode is None:
            status = "ERROR"
            reason = reason or "driver did not run"
        else:
            status = "UNKNOWN"
            reason = reason or "driver exited 0 without a status marker"
    origin = origin_for(status, stage, g_status) if status != "UNKNOWN" else None
    return DriverOutcome(status=status, error_origin=origin, stage=stage, reason=reason,
                         gurobi_line=g_line, warnings=list(markers["warnings"]))


# ---------------------------------------------------------------------------
# Output archiving
# ---------------------------------------------------------------------------

def year_agnostic_name(filename: str, year: int) -> str:
    """``summary_national_2060.csv`` -> ``summary_national.csv``."""
    return re.sub(rf"_{int(year)}(?=\.csv$)", "", filename)


def archive_outputs(results_year_dir: str | os.PathLike, dst: str | os.PathLike, year: int) -> List[str]:
    """Copy top-level CSVs from ``<results>/outputs`` and
    ``<results>/outputs_processed`` into ``dst`` with year-agnostic names.
    Returns the archived file names (sorted). Per-province sub-folders (hourly
    series, shadow prices) stay in the workspace."""
    src = Path(results_year_dir)
    dst = Path(dst)
    archived: List[str] = []
    if not src.is_dir():
        return archived
    dst.mkdir(parents=True, exist_ok=True)
    for sub in ("outputs", "outputs_processed"):
        folder = src / sub
        if not folder.is_dir():
            continue
        for csv in sorted(folder.glob("*.csv")):
            if not csv.is_file():
                continue
            name = year_agnostic_name(csv.name, year)
            if name in archived:
                name = f"{sub}_{name}"
            shutil.copy2(csv, dst / name)
            archived.append(name)
    return sorted(archived)


# ---------------------------------------------------------------------------
# Human-readable schema (the adapter's describe_config)
# ---------------------------------------------------------------------------

#: One line per config key: (key, tier, default, what it does / where the model
#: consumes it). Every claim carries a file:line citation into models/pathways
#: so a reader can check it. Keep in sync with DEFAULTS / TIER_*_KEYS.
KEY_DOCS: List[Tuple[str, str, str, str]] = [
    ("year", "B", "required",
     "Modelled year (2030/2040/2050/2060). Selects the input decade produced by "
     "MultiYearAutomation and the emission-target row (main.py:1333). Also picks the "
     "capex point on the cost trajectory (testMultiYear.py:112-148)."),
    ("optimization_days", "B", "5",
     "How many representative days are sampled from the 8760-h year "
     "(scen_params.optimization_hours.days; initData.seedHour:65). Cost scales roughly "
     "linearly; it is the main runtime dial. Also multiplies the hard-coded 5 % thermal "
     "minimum-capacity-factor constraint (main.py:1345-1358)."),
    ("optimization_step", "B", "10",
     "Spacing in days between sampled days (scen_params.optimization_hours.step). "
     "(days-1)*step must be <= 364 or seedHour silently drops hours past 8759 "
     "(initData.py:65-72)."),
    ("vre_year", "B", '"w2015_s2015"',
     "Weather year pair for wind/solar capacity factors. Only 2015 ships with the "
     "Zenodo data, so this is effectively fixed; it is part of the results folder name."),
    ("emission_target", "C", '"2C"',
     "POLICY. Chooses data_csv/capacity_assumptions/power_sector_emission_{2C|15C}.csv, "
     "the annual CO2 cap (main.py:1277-1341). 2060: 2C = -563.47 Mt, 15C = -700 Mt. "
     "Also drives the national demand trajectory (multiYearAutomation.py:130-137). "
     "Switching 15C -> 2C loosens the cap: never auto-apply."),
    ("emission_cap_override_mt", "C", "unset",
     "POLICY. When set, the driver materialises a workspace copy of the emission CSV "
     "with this value (Mt CO2) on the row for `year`; the model then treats it as the "
     "cap. Present so the guardrail has a direct, unambiguous policy lever. NOTE: a "
     "negative value is clamped to 0 by the model when ccs_start_year > year "
     "(main.py:1334-1336)."),
    ("comply_with_medium_vre_goal", "C", "0",
     "POLICY. 1 forces >= 1200 GW of new wind+solar in 2030 (main.py:1265-1272). Inert "
     "in any other year. Turning it off to reach feasibility removes a policy floor."),
    ("ccs_start_year", "C", "2040",
     "POLICY. Before this year all CCS capacities are pinned to 0 (main.py:477, 527) and "
     "a negative emission target is clamped to 0 (main.py:1334). Bringing CCS forward is "
     "a policy assumption, not a numeric fix."),
    ("with_shedding", "C", "0",
     "POLICY. 1 enables load-shedding variables in the energy balance and reserve "
     "constraints (main.py:414, 773, 1010, 1030, 1125). Enabling it makes almost any "
     "infeasible demand case feasible by letting the model drop load — the archetypal "
     "silent relaxation."),
    ("demand_resv", "C", "0.05",
     "POLICY. Firm-capacity reserve margin on demand (main.py:329, 1139, 1186)."),
    ("vre_resv", "C", "0.05",
     "POLICY. Reserve requirement against VRE output (main.py:328, 1152, 1197)."),
    ("wind_with_xz", "C", "0",
     "POLICY. 0 excludes Tibet (Xizang) wind, matching practice (main.py:360). Enabling "
     "it adds resource that does not exist in the study's assumptions."),
    ("renewable_cost_decline", "B", '"baseline"',
     '"baseline" (linear 2025->2060) or "conservative" (NREL 2023 trajectory) capex for '
     "on/offshore wind, utility and distributed PV, battery, CAES, VRB "
     "(testMultiYear.py:112-148)."),
    ("demand_sensitivity", "B", '"none"',
     '"p5"/"m5" scale the national demand trajectory by +/-5 % '
     "(multiYearAutomation.py:147-152); \"none\" leaves it alone."),
    ("demand_scale", "B", "1.0",
     "Multiplier on every provincial hourly demand series. The model reads "
     "scen_params.demand.scale into `alpha` (initData.py:949) but never applies it — the "
     "block that used alpha is commented out (initData.py:1006-1013) — so the driver "
     "instead rewrites the per-run workspace's provin_demand_hourly/*.csv. Both the "
     "scen_params key and the CSVs are set, so the provenance is visible."),
    ("heating_electrification", "B", '"chp_ccs"',
     '"chp_ccs" keeps coal CHP with CCS; "heat_pump" electrifies heat instead '
     "(main.py:479)."),
    ("endogenize_firm_capacity", "B", "1",
     "1 lets the optimizer choose firm (thermal) capacity; 0 pins it to the exogenous "
     "plan (main.py:457)."),
    ("ccs_retrofit_cost", "B", "3500",
     "RMB/kW capex for coal-CCS and gas-CCS retrofits (scen_params.ccs.capex_coal_ccs and "
     "capex_gas_ccs; testMultiYear.py:110-111)."),
    ("wacc", "B", "7.4",
     "Weighted average cost of capital in percent; drives every CRF "
     "(scen_params.finance.weighted_average_cost_of_capital, initData.getCRF)."),
    ("time_limit", "A", "unset",
     "Gurobi TimeLimit in seconds, written into the run's gurobi.env. On expiry the log "
     "says 'Time limit reached'; the model then raises reading .objVal, which the driver "
     "catches and reports as TIME_LIMIT."),
    ("threads", "A", "unset", "Gurobi Threads."),
    ("bar_conv_tol", "A", "unset",
     "Gurobi BarConvTol — barrier convergence tolerance. The model forces Method=2 "
     "(barrier) and Crossover=0 (main.py:347-348), so this is the tolerance that matters."),
    ("numeric_focus", "A", "unset", "Gurobi NumericFocus (0-3); raise it on numerical trouble."),
    ("res_tag", "A", '"agentic"',
     "Label only: the results folder is data_res/<res_tag>_<vre_year>/<year>/. The adapter "
     "injects the run directory's name when the config has none, so concurrent runs never "
     "collide."),
]


def describe_schema() -> str:
    """Full human-readable schema for the scenario-builder / refiner skills."""
    lines = [
        "Adapter 'pathways' — China provincial-resolution renewable-energy pathways "
        "model (models/pathways, pinned 99f94de). A single-year, hourly, "
        "province-resolved capacity-expansion + dispatch LP solved with Gurobi "
        "(barrier, no crossover).",
        "",
        "HOW IT RUNS. The model has no config-file entry point: its scripts mutate a "
        "`scen_params` dict in Python and read/write everything relative to a "
        "`work_dir` derived from where pycode/ sits (callUtility.getWorkDir). The "
        "adapter therefore never edits the model. It builds a per-run workspace of "
        "symlinks inside the run directory (pycode -> the model, data_pkl/data_mat/"
        "data_shp -> the Zenodo data, data_csv mirrored entry by entry, a real "
        "data_res/), which relocates work_dir into the run directory, and replays the "
        "single-year flow of pycode/testSingleYear.py with this config applied onto "
        "data_csv/scen_params_template.json. All outputs land under the run directory.",
        "",
        "CONFIG. A flat JSON object; `year` is the only required key. Every other key "
        "is optional and falls back to the default below (which equals the model's own "
        "template). Unknown keys are rejected by validate_config, and the guardrail "
        "classifies any unknown key as Tier C.",
        "",
        "KEYS (key [tier] default — meaning):",
    ]
    for key, tier, default, doc in KEY_DOCS:
        lines.append(f"  - {key} [{tier}] default {default} — {doc}")
    lines += [
        "",
        "ENUMERATED VALUES:",
    ]
    for key in sorted(ALLOWED_VALUES):
        lines.append(f"  - {key}: {ALLOWED_VALUES[key]}")
    lines += [
        "",
        "TIERS (the scientific-integrity guardrail).",
        f"  A / auto-apply (solver numerics + labels): {sorted(TIER_A_KEYS)}",
        f"  B / auto-apply + flag (sanctioned parameters): {sorted(TIER_B_KEYS)}",
        f"  C / human sign-off (policy constraints): {sorted(TIER_C_KEYS)}",
        "  Tier C is where this model's scientific claim lives: the CO2 cap "
        "(emission_target / emission_cap_override_mt), the 2030 VRE build floor "
        "(comply_with_medium_vre_goal), when CCS becomes available (ccs_start_year), "
        "the reserve margins (demand_resv / vre_resv), the Tibet wind exclusion "
        "(wind_with_xz), and above all with_shedding — turning load shedding on will "
        "make almost any infeasible case 'solve' by dropping demand. None of these may "
        "be relaxed to reach feasibility without a human saying so.",
        "",
        "STATUS. gurobipy's log is the only source of truth: main.py:1379 reads "
        "`.objVal` with no status check, so an infeasible or time-limited solve raises "
        "a GurobiError. The driver catches it and reports "
        "OPTIMAL / INFEASIBLE / TIME_LIMIT / ERROR from the log, exiting 0 for the "
        "first three (a failed solve is a result, not a crash).",
        "",
        "OUTPUTS (archived into <run_dir>/outputs/ with the year stripped from the "
        "file names). objValue.csv: headerless 'item,million RMB,Var/Constant' rows, "
        "'objValue' first, then every cost component. emissionValue.csv: headerless "
        "'name,Mt' rows — emission_target_mt (the binding cap, after the "
        "ccs_start_year clamp) plus emission_{beccs,gas_unabated,gas_ccs,"
        "coal_unabated,coal_ccs,chp_ccs}_mt, whose sum is the left-hand side of the "
        "cap constraint. emissionBreakdowns.csv: per-province capacity, generation, "
        "capacity factor and emissions by firm technology. summary_national.csv / "
        "summary_provincial.csv: demand, capacity and land-use rollups. Also "
        "ws_capacity_pro.csv (wind/solar GW by province), integrated_storage.csv and "
        "trans_cap.csv (headerless).",
        "",
        "COST. A single year at 5 sampled days is the smallest useful run; the "
        "README's 1-week-per-decade multi-year demo is ~30 min on 10 cores, and a full "
        "8760-h year can take up to 20 h on 32 cores. Requires a Gurobi licence and "
        "the Zenodo data set (PATHWAYS_DATA_ROOT).",
    ]
    return "\n".join(lines)
