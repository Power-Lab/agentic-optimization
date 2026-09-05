"""Second real-model adapter: the China renewable-energy Pathways model.

Wires the framework's adapter contract to ``models/pathways`` (a git submodule,
never modified) — a provincial-resolution, hourly capacity-expansion + dispatch
LP for China's power sector, solved with Gurobi.

The model has no config-file entry point: its ``pycode/test*.py`` scripts mutate
a ``scen_params`` dict in Python, and ``callUtility.getWorkDir`` derives every
input/output path from where ``pycode/`` sits. So this adapter is an **external
config-injection shim**:

    config.json ──► adapters/pathways/driver.py (run under $PATHWAYS_PYTHON)
                      ├─ builds <run_dir>/workspace/ of symlinks, which
                      │  relocates the model's work_dir into the run directory
                      ├─ applies the config onto scen_params_template.json
                      ├─ replays the single-year flow of testSingleYear.py
                      └─ prints "[pathways] status: ..." and writes
                         driver_result.json

Everything reusable and testable — the schema, the config→scen_params mapping,
the workspace builder, Gurobi log parsing, output archiving — lives in
``shim.py``, which is stdlib-only and imported by *both* interpreters. This
module is the framework-side half: subprocess, streaming, run record.

Nothing here edits the model, and nothing here writes outside ``run_dir``.
"""

from __future__ import annotations

import json
import os
import shutil
import time
from dataclasses import fields
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from framework.adapter import Adapter, ValidationResult
from framework.interventions import InterventionSpec
from framework.run_record import Execution

try:  # the framework's streamer: tee + ticks + abort file, shared by all adapters
    from framework.process import stream_command
except ImportError:  # pragma: no cover - only if framework/process.py is missing
    stream_command = None  # type: ignore[assignment]

from adapters.pathways import shim
from adapters.pathways.shim import (  # re-exported for tests / examples
    ALLOWED_VALUES,
    DEFAULTS,
    SMOKE_CONFIG,
    TIER_A_KEYS,
    TIER_B_KEYS,
    TIER_C_KEYS,
    describe_schema,
)

# Repo layout: <framework-root>/adapters/pathways/pathways_adapter.py
FRAMEWORK_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_MODEL_ROOT = FRAMEWORK_ROOT / "models" / "pathways"
DRIVER_PATH = Path(__file__).resolve().parent / "driver.py"
MODEL_REPO_URL = "https://github.com/Power-Lab/AdvAppliedEnergy_Pathways_2025.git"

#: The conda env the build brief provisions (gurobipy 10.0.3 + pandas + geopandas).
_CONDA_PYTHON = Path.home() / "miniforge3" / "envs" / "agentic-pathways" / "bin" / "python"

#: Whether ``Execution`` carries the live-monitor summary (Workstream D). Kept
#: as a probe so this adapter works against either version of the dataclass.
_EXECUTION_HAS_MONITOR = any(f.name == "monitor" for f in fields(Execution))


def default_python() -> str:
    """Interpreter that runs the driver: ``$PATHWAYS_PYTHON``, else the
    provisioned conda env, else whatever ``python3`` is on PATH."""
    env = os.environ.get("PATHWAYS_PYTHON")
    if env:
        return env
    if _CONDA_PYTHON.exists():
        return str(_CONDA_PYTHON)
    return "python3"


def default_data_root() -> Optional[str]:
    """Root holding the unpacked Zenodo ``data_pkl/``, ``data_mat/``,
    ``data_shp/``. There is no sensible fallback — without it nothing can run."""
    return os.environ.get("PATHWAYS_DATA_ROOT") or None


class PathwaysAdapter(Adapter):
    """Local execution: run the driver as a subprocess on this machine."""

    name = "pathways"

    def __init__(
        self,
        model_root: str | Path = DEFAULT_MODEL_ROOT,
        data_root: str | Path | None = None,
        python: Optional[str] = None,
        driver: str | Path = DRIVER_PATH,
        extra_env: Optional[Dict[str, str]] = None,
        wall_timeout: Optional[float] = None,
    ):
        self.model_root = Path(model_root)
        raw_data_root = data_root if data_root is not None else default_data_root()
        self.data_root: Optional[Path] = (
            Path(raw_data_root).expanduser() if raw_data_root else None
        )
        self.python = python or default_python()
        self.driver = Path(driver)
        self.extra_env: Dict[str, str] = dict(extra_env or {})
        # Belt-and-braces wall clock for the whole subprocess. Gurobi's own
        # ``time_limit`` is the right lever; this only catches a wedged driver.
        self.wall_timeout = wall_timeout

    # ---- validation ------------------------------------------------------

    @staticmethod
    def validate_schema(config: Dict[str, Any]) -> List[str]:
        """Filesystem-free schema check (shared with the driver and the remote
        backend), so a config can be vetted before anything is provisioned."""
        return shim.validate_schema(config)

    def validate_config(self, config: Dict[str, Any]) -> ValidationResult:
        """Schema + the things that must exist before a run can start: the model
        checkout, the Zenodo data root, and the driver script. The interpreter is
        checked only for existence when it looks like a path — importing gurobipy
        is left to the run itself, where the failure is visible in the log."""
        errors: List[str] = self.validate_schema(config)
        errors += shim.check_model_root(self.model_root)
        errors += shim.check_data_root(self.data_root)
        if not self.driver.is_file():
            errors.append(f"driver script not found: {self.driver}")
        if os.sep in self.python and not Path(self.python).expanduser().exists():
            errors.append(
                f"python interpreter not found: {self.python} "
                f"(set PATHWAYS_PYTHON to the env that has gurobipy + pandas)"
            )
        return ValidationResult(ok=not errors, errors=errors)

    # ---- execution -------------------------------------------------------

    def run(
        self,
        config: Dict[str, Any],
        run_dir: Path,
        on_event: Optional[Callable[..., Any]] = None,
        on_line: Optional[Callable[[str], None]] = None,
    ) -> Execution:
        """Execute one single-year run; never raises on a failed solve.

        Writes ``config.json`` (the *executed* config, with ``res_tag`` injected)
        and ``solver.log`` into ``run_dir``, archives the model's result CSVs into
        ``run_dir/outputs/``, and returns the :class:`Execution`.
        """
        run_dir = Path(run_dir)
        run_dir.mkdir(parents=True, exist_ok=True)
        executed = self.executed_config(config, run_dir)
        self._write_config(executed, run_dir)
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

        result = self._stream(self.command(run_dir), log_path, sink, run_dir, monitor)
        returncode = result.returncode
        wall = float(result.wall_seconds)
        timed_out = bool(getattr(result, "timed_out", False))
        text = "".join(lines)

        outcome = shim.parse_driver_output(
            text, returncode,
            gurobi_log=self.gurobi_log_text(run_dir),
            driver_result=self.read_driver_result(run_dir),
        )
        status = outcome.status
        origin = outcome.error_origin
        if timed_out:
            # The adapter's own wall clock fired, not Gurobi's TimeLimit.
            status, origin = "TIME_LIMIT", "runtime"

        archived = self.archive_outputs(executed, run_dir)
        if status == "OPTIMAL" and not archived:
            status, origin = "ERROR", "runtime"

        execution = Execution(
            termination_status=status,
            wall_seconds=round(wall, 1),
            solver_log="solver.log",
            returncode=returncode,
            error_origin=origin,
        )
        if monitor is not None:
            summary = self._finish_monitor(monitor, result)
            if summary is not None and _EXECUTION_HAS_MONITOR:
                execution.monitor = summary  # type: ignore[attr-defined]
        return execution

    # ---- adapter contract ------------------------------------------------

    def intervention_spec(self) -> InterventionSpec:
        return InterventionSpec(
            # A — Gurobi numerics and the results-folder label.
            tier_a_keys=set(TIER_A_KEYS),
            # B — sanctioned scenario/technoeconomic parameters and the horizon.
            tier_b_keys=set(TIER_B_KEYS),
            # C — the policy constraints the study's claim rests on.
            tier_c_keys=set(TIER_C_KEYS),
            allowed_values={k: list(v) for k, v in ALLOWED_VALUES.items()},
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
        """The config actually handed to the driver: the caller's config plus an
        injected ``res_tag`` (the run dir's basename, sanitised) when it has none,
        so two runs never share a results folder name. The framework's
        ``record.config`` keeps the caller's config untouched."""
        executed = dict(config)
        if not str(executed.get("res_tag", "") or "").strip():
            executed["res_tag"] = shim.sanitize_tag(Path(run_dir).name)
        return executed

    def command(self, run_dir: str | Path) -> List[str]:
        run_dir = Path(run_dir)
        cmd = [self.python, str(self.driver),
               "--config", str(run_dir / "config.json"),
               "--run-dir", str(run_dir),
               "--model-root", str(self.model_root)]
        if self.data_root is not None:
            cmd += ["--data-root", str(self.data_root)]
        return cmd

    def env(self) -> Dict[str, str]:
        env = dict(os.environ)
        if self.data_root is not None:
            env["PATHWAYS_DATA_ROOT"] = str(self.data_root)
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        env.update(self.extra_env)
        return env

    @staticmethod
    def _write_config(config: Dict[str, Any], run_dir: Path) -> Path:
        cfg_path = Path(run_dir) / "config.json"
        cfg_path.write_text(json.dumps(config, indent=2))
        return cfg_path

    # -- run artefacts -----------------------------------------------------

    @staticmethod
    def workspace_dir(run_dir: str | Path) -> Path:
        return Path(run_dir) / "workspace"

    @classmethod
    def gurobi_log_text(cls, run_dir: str | Path) -> str:
        """The solver's own ``LogFile`` (written inside the workspace). Empty
        when the run never reached the solver."""
        path = cls.workspace_dir(run_dir) / "gurobi.log"
        try:
            return path.read_text(errors="replace") if path.is_file() else ""
        except OSError:
            return ""

    @staticmethod
    def read_driver_result(run_dir: str | Path) -> Optional[Dict[str, Any]]:
        path = Path(run_dir) / "driver_result.json"
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError):
            return None
        return data if isinstance(data, dict) else None

    def results_dir(self, config: Dict[str, Any], run_dir: str | Path) -> Path:
        """Where the model wrote this run's outputs, inside the workspace. The
        driver reports it in ``driver_result.json``; otherwise it is derived from
        the config the same way ``multiYearAutomation`` builds it."""
        result = self.read_driver_result(run_dir)
        reported = (result or {}).get("results_dir")
        if reported:
            return Path(reported)
        return self.workspace_dir(run_dir) / shim.results_relpath(config)

    def archive_outputs(self, config: Dict[str, Any], run_dir: str | Path) -> List[str]:
        """Copy the run's result CSVs out of the workspace into
        ``<run_dir>/outputs/`` with year-agnostic names, so the record survives
        the workspace being deleted."""
        return shim.archive_outputs(
            self.results_dir(config, run_dir),
            Path(run_dir) / "outputs",
            int(shim.effective_config(config)["year"]),
        )

    @staticmethod
    def cleanup_workspace(run_dir: str | Path) -> bool:
        """Delete ``<run_dir>/workspace`` once its outputs are archived. The
        workspace is mostly symlinks, but the model's per-province hourly CSVs
        under ``data_res/`` are real and add up across a sweep."""
        ws = PathwaysAdapter.workspace_dir(run_dir)
        if not ws.is_dir():
            return False
        shutil.rmtree(ws)
        return True

    # -- streaming / monitoring -------------------------------------------

    def _stream(self, cmd: Sequence[str], log_path: Path,
                on_line: Callable[[str], None],
                run_dir: Path, monitor: Any = None) -> Any:
        """Run ``cmd``, tee merged stdout/stderr into ``log_path`` line by line,
        and return the stream result (``returncode`` / ``wall_seconds``, plus
        ``timed_out`` / ``aborted`` / ``abort_reason`` on the framework path).

        With a ``monitor``, ``framework.process.stream_command`` also drives
        ``monitor.tick()`` while Gurobi is quiet (a barrier LP can run for
        minutes between log lines) and honours ``monitor.abort_event``.
        ``stream_local`` is the fallback for a checkout without
        ``framework/process.py``; it enforces no wall timeout.
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
            return stream_command(list(cmd), cwd=str(run_dir), log_path=str(log_path),
                                  env=self.env(), on_line=on_line,
                                  timeout=self.wall_timeout, **kwargs)
        rc, wall = stream_local(cmd, cwd=run_dir, log_path=log_path,
                                env=self.env(), on_line=on_line)
        return SimpleNamespace(returncode=rc, wall_seconds=wall,
                               timed_out=False, aborted=False, abort_reason=None)

    @staticmethod
    def _make_monitor(config: Dict[str, Any], run_dir: Path, on_event: Any) -> Any:
        """Wrap ``on_event`` in a ``framework.monitor.RunMonitor`` when that
        module (Workstream D) is importable. ``on_event`` may also already *be* a
        monitor. Returns None when monitoring is not requested or unavailable —
        a monitoring failure must never fail the run."""
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
        ``aborted`` are recorded). Monitoring never fails a run."""
        finish = getattr(monitor, "finish", None)
        if finish is None:
            return None
        try:
            summary = finish(result)
        except Exception:
            return None
        return summary if isinstance(summary, dict) else None


def stream_local(cmd: Sequence[str], cwd: str | Path, log_path: str | Path,
                 env: Optional[Dict[str, str]] = None,
                 on_line: Optional[Callable[[str], None]] = None) -> Tuple[Optional[int], float]:
    """Minimal Popen tee, used only when ``framework.process.stream_command``
    is unavailable. Merged stdout/stderr, flushed to ``log_path`` per line."""
    import subprocess

    start = time.monotonic()
    with open(log_path, "w", buffering=1) as log:
        proc = subprocess.Popen([str(c) for c in cmd], cwd=str(cwd), env=env,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, bufsize=1)
        assert proc.stdout is not None
        for line in proc.stdout:
            log.write(line)
            log.flush()
            if on_line is not None:
                on_line(line)
        proc.wait()
    return proc.returncode, time.monotonic() - start
