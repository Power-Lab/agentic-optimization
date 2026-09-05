#!/usr/bin/env python
"""Standalone driver: one single-year run of the China RE Pathways model.

Launched by ``PathwaysAdapter.run`` as a subprocess under ``PATHWAYS_PYTHON``
(the model's conda env with gurobipy/pandas/geopandas). It never edits the
model. Instead it:

1. builds a per-run **workspace** (``<run_dir>/workspace``) of symlinks so the
   model's ``work_dir`` relocates there (see ``shim.build_workspace``);
2. ``chdir``s into it (Gurobi reads ``gurobi.env`` from the CWD) and inserts
   ``workspace/pycode`` at the head of ``sys.path``;
3. replays the single-year flow of ``pycode/testSingleYear.py`` with the config
   applied onto ``scen_params_template.json`` (``shim.build_scen_params``);
4. prints marker lines ``[pathways] stage: <name>`` / ``[pathways] status: <S>``
   / ``[pathways] reason: ...`` that the adapter parses, and writes
   ``<run_dir>/driver_result.json``.

Exit code: 0 for OPTIMAL / INFEASIBLE / TIME_LIMIT (a failed solve is a normal
result), 1 for ERROR. The Gurobi log is streamed to stdout by gurobipy and also
written in full to ``workspace/gurobi.log`` (``LogFile`` parameter).

This file must stay importable by the *model's* interpreter alone: it imports
``shim.py`` by path (never ``adapters``/``framework``), so nothing framework-side
is dragged into the conda env.

Usage:
    python driver.py --config <config.json> --run-dir <run_dir>
                     --model-root <models/pathways> [--data-root <zenodo root>]
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
import time
import traceback
from pathlib import Path
from typing import Any, Dict, Optional

HERE = Path(__file__).resolve().parent

# The single-year flow uses "no previous decade" (testSingleYear.py: last_year = 2020
# for the first modelled year), so initCellData falls back to the model's default
# integrated_{wind,solar}.csv (initData.py:344-352) and the run is self-contained.
LAST_YEAR = 2020
MULTIYEAR_ARGS = dict(yr_start=2025, yr_end=2060, yr_step=5)
SHIM_MODULE_NAME = "pathways_shim"


def _load_shim():
    """Load ``shim.py`` by path so no ``adapters``/``framework`` package import
    (and none of its side effects) happens inside the model's environment.

    The module must be registered in ``sys.modules`` *before* it is executed:
    ``dataclasses`` resolves string annotations through
    ``sys.modules[cls.__module__]`` and raises ``AttributeError: 'NoneType'
    object has no attribute '__dict__'`` when the module is not there.
    """
    path = HERE / "shim.py"
    spec = importlib.util.spec_from_file_location(SHIM_MODULE_NAME, path)
    if spec is None or spec.loader is None:  # pragma: no cover - defensive
        raise ImportError(f"cannot load the pathways shim from {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[SHIM_MODULE_NAME] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(SHIM_MODULE_NAME, None)
        raise
    return module


def say(kind: str, msg: Any = "") -> None:
    print(f"[pathways] {kind}: {msg}", flush=True)


def _print_exc() -> str:
    text = traceback.format_exc()
    print(text, flush=True)
    last = [ln for ln in text.strip().splitlines() if ln.strip()]
    return last[-1] if last else "exception"


class Driver:
    def __init__(self, shim, config: Dict[str, Any], run_dir: Path, model_root: Path,
                 data_root: Optional[Path]):
        self.shim = shim
        self.raw_config = config
        self.cfg = shim.effective_config(config)
        self.run_dir = run_dir
        self.model_root = model_root
        self.data_root = data_root
        self.ws_root = run_dir / "workspace"
        self.result: Dict[str, Any] = {
            "status": None, "reason": None, "stage": None, "gurobi_line": None,
            "wall_seconds": None, "solve_seconds": None, "workspace": str(self.ws_root),
            "results_dir": None, "overrides": [], "gurobi_env": None,
            "demand_scaled_files": 0, "n_hours": None, "postprocess_errors": [],
        }
        self.t0 = time.monotonic()

    # -- helpers ----------------------------------------------------------

    def stage(self, name: str) -> None:
        self.result["stage"] = name
        say("stage", name)

    def finish(self, status: str, reason: str) -> int:
        self.result["status"] = status
        self.result["reason"] = reason
        self.result["wall_seconds"] = round(time.monotonic() - self.t0, 1)
        say("status", status)
        say("reason", reason)
        try:
            (self.run_dir / "driver_result.json").write_text(json.dumps(self.result, indent=2))
        except OSError as exc:  # never mask the status over a bookkeeping failure
            say("warning", f"could not write driver_result.json: {exc}")
        return 0 if status in ("OPTIMAL", "INFEASIBLE", "TIME_LIMIT") else 1

    def gurobi_log_text(self) -> str:
        path = self.ws_root / "gurobi.log"
        try:
            return path.read_text(errors="replace") if path.is_file() else ""
        except OSError:
            return ""

    # -- stages -----------------------------------------------------------

    def run(self) -> int:
        shim = self.shim
        cfg = self.cfg

        self.stage("config")
        errors = shim.validate_schema(self.raw_config)
        if errors:
            return self.finish("ERROR", "config rejected: " + "; ".join(errors))
        say("config", json.dumps(cfg, sort_keys=True))

        # 1) workspace ------------------------------------------------------
        self.stage("workspace")
        override = None
        if cfg.get("emission_cap_override_mt") is not None:
            override = (cfg["emission_target"], int(cfg["year"]), cfg["emission_cap_override_mt"])
        params = shim.gurobi_params(cfg, log_file=self.ws_root / "gurobi.log")
        try:
            ws = shim.build_workspace(self.ws_root, self.model_root, self.data_root,
                                      emission_override=override, gurobi_params=params)
        except shim.WorkspaceError as exc:
            return self.finish("ERROR", f"workspace: {exc}")
        except Exception:
            return self.finish("ERROR", f"workspace: {_print_exc()}")
        self.result["overrides"] = list(ws.overrides)
        for line in ws.overrides:
            say("override", line)
        if ws.gurobi_env is not None:
            env_text = ws.gurobi_env.read_text()
            self.result["gurobi_env"] = env_text
            say("gurobi_env", env_text.replace("\n", " | ").strip(" |"))

        os.chdir(ws.root)
        sys.path.insert(0, str(ws.pycode))

        # 2) import the model through the workspace -------------------------
        self.stage("import")
        try:
            import pandas as pd  # noqa: F401  (model dependency)
            from callUtility import getWorkDir, VreYearSplit
            from multiYearAutomation import MultiYearAutomation
            from initData import seedHour, initCellData, initDemLayer, initModelExovar
            from main import interProvinModel
            from clearupData import (cellResInfo, TransCap, update_storage_capacity,
                                     obtain_output_summary)
        except Exception:
            return self.finish("ERROR", f"import: {_print_exc()}")

        work_dir = getWorkDir()
        say("work_dir", work_dir)
        expected = str(ws.root)
        if os.path.normpath(work_dir) != os.path.normpath(expected):
            # Refuse to proceed: the model would read/write outside the workspace
            # (e.g. into the submodule) — never acceptable.
            return self.finish(
                "ERROR",
                f"work_dir mismatch: model resolved {work_dir!r}, expected {expected!r}; aborting "
                f"before any write",
            )

        # 3) inputs ---------------------------------------------------------
        self.stage("inputs")
        year = int(cfg["year"])
        vre_year = cfg["vre_year"]
        res_tag = cfg["res_tag"]
        try:
            auto = MultiYearAutomation(res_tag=res_tag, vre_year=vre_year,
                                       emission_target=cfg["emission_target"],
                                       demand_sensitivity=cfg["demand_sensitivity"],
                                       **MULTIYEAR_ARGS)
            auto.automate_inputs()

            results_dir = Path(auto.out_path) / str(year)
            self.result["results_dir"] = str(results_dir)
            say("results_dir", results_dir)
            out_input_path = results_dir / "inputs"

            # 3a) demand_scale — see shim.scale_demand_csv for why this is done
            # on the workspace CSVs instead of scen_params["demand"]["scale"].
            scale = float(cfg["demand_scale"])
            if scale != 1.0:
                self.stage("demand_scale")
                touched = shim.scale_demand_dir(results_dir / shim.DEMAND_DIRNAME, scale)
                self.result["demand_scaled_files"] = len(touched)
                say("warning", f"demand_scale={scale} applied to {len(touched)} provincial "
                               f"hourly demand files in the run workspace (the model reads "
                               f"scen_params.demand.scale but never applies it)")
                if not touched:
                    return self.finish("ERROR", "demand_scale: no provincial demand CSVs were "
                                                "scaled; the workspace demand folder is empty")
                self.stage("inputs")

            template = json.loads((ws.data_csv / "scen_params_template.json").read_text())
            scen_params = shim.build_scen_params(template, self.raw_config)
            (out_input_path / "scen_params.json").write_text(json.dumps(scen_params))

            seedHour(vre_year=vre_year,
                     years=scen_params["optimization_hours"]["years"],
                     step=scen_params["optimization_hours"]["step"],
                     days=scen_params["optimization_hours"]["days"],
                     res_tag=res_tag, curr_year=year)
            hour_seed = pd.read_csv(out_input_path / "hour_seed.csv", header=None).iloc[:, 0].to_list()
            self.result["n_hours"] = len(hour_seed)
            say("info", f"{len(hour_seed)} optimisation hours "
                        f"({cfg['optimization_days']} days, step {cfg['optimization_step']})")

            initDemLayer(vre_year=vre_year, res_tag=res_tag, curr_year=year, scen_params=scen_params)

            wind_year, solar_year = VreYearSplit(vre_year)
            for vre, single in (("solar", solar_year), ("wind", wind_year)):
                initCellData(vre=vre, vre_year_single=single, hour_seed=hour_seed, res_tag=res_tag,
                             vre_year=vre_year, curr_year=year, last_year=LAST_YEAR,
                             scen_params=scen_params)

            initModelExovar(vre_year=vre_year, res_tag=res_tag, curr_year=year,
                            last_year=LAST_YEAR, scen_params=scen_params)
        except Exception:
            return self.finish("ERROR", f"inputs: {_print_exc()}")

        # 4) solve ----------------------------------------------------------
        self.stage("solve")
        solve_error: Optional[str] = None
        t_solve = time.monotonic()
        try:
            interProvinModel(vre_year=vre_year, res_tag=res_tag, init_data=0, is8760=0,
                             curr_year=year, scen_params=scen_params)
        except Exception:
            # An infeasible/time-limited solve surfaces here: main.py reads .objVal
            # with no status check and gurobipy raises. The log carries the truth.
            solve_error = _print_exc()
        self.result["solve_seconds"] = round(time.monotonic() - t_solve, 1)

        status, line = self.shim.parse_gurobi_status(self.gurobi_log_text())
        self.result["gurobi_line"] = line
        if status is None:
            return self.finish("ERROR", f"solve: {solve_error or 'no Gurobi termination marker in the log'}")
        if status != "OPTIMAL":
            return self.finish(status, f"Gurobi: {line}" + (f" ({solve_error})" if solve_error else ""))
        if solve_error:
            return self.finish("ERROR", f"solve reached OPTIMAL but the model raised while writing "
                                        f"outputs: {solve_error}")

        # 5) post-process ---------------------------------------------------
        # Order matters: obtain_output_summary reads ws_capacity_pro.csv +
        # {wind,solar}_info.csv (cellResInfo), trans_cap.csv (TransCap) and
        # integrated_storage_<year>.csv (update_storage_capacity).
        self.stage("postprocess")
        for fn in (cellResInfo, TransCap, update_storage_capacity, obtain_output_summary):
            try:
                fn(vre_year=vre_year, res_tag=res_tag, curr_year=year)
            except Exception:
                msg = f"{fn.__name__} failed: {_print_exc()}"
                self.result["postprocess_errors"].append(msg)
                say("warning", msg)

        self.stage("done")
        return self.finish("OPTIMAL", f"Gurobi: {line}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", required=True, help="flat adapter config.json")
    ap.add_argument("--run-dir", required=True, help="run directory (workspace/ is created inside)")
    ap.add_argument("--model-root", required=True, help="path to the pathways model checkout")
    ap.add_argument("--data-root", default=os.environ.get("PATHWAYS_DATA_ROOT"),
                    help="folder holding data_pkl/, data_mat/, data_shp/ (default: $PATHWAYS_DATA_ROOT)")
    args = ap.parse_args(argv)

    # Never litter the submodule with bytecode when importing through the symlink.
    sys.dont_write_bytecode = True
    os.environ.setdefault("PYTHONDONTWRITEBYTECODE", "1")
    try:
        sys.stdout.reconfigure(line_buffering=True)  # type: ignore[attr-defined]
    except Exception:
        pass

    shim = _load_shim()
    run_dir = Path(args.run_dir).expanduser().resolve()
    run_dir.mkdir(parents=True, exist_ok=True)
    try:
        config = json.loads(Path(args.config).read_text())
    except Exception as exc:
        say("stage", "config")
        say("status", "ERROR")
        say("reason", f"could not read config: {exc}")
        return 1

    # Absolute paths only: the driver chdirs into the workspace, after which a
    # relative model/data root would resolve somewhere else entirely.
    data_root = Path(args.data_root).expanduser().resolve() if args.data_root else None
    driver = Driver(shim, config, run_dir, Path(args.model_root).expanduser().resolve(), data_root)
    try:
        return driver.run()
    except Exception:
        # Last line of defence: still emit a status marker.
        return driver.finish("ERROR", f"unhandled: {_print_exc()}")


if __name__ == "__main__":
    raise SystemExit(main())
