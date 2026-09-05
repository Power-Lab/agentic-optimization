"""``PypsaToyAdapter`` — the framework's fully-reproducible third model.

Unlike the two lab models, this one needs no licence, no institutional dataset
and no cluster: a deterministic 3-bus capacity-expansion LP built by
``adapters.pypsa_toy.network`` and solved by HiGHS in seconds. It exists so
the guardrail experiment has a leg anyone can rerun, and so the framework's
contract (tiers, run records, streamed logs, archived outputs) is exercised
end to end on a model that fits on one screen.

Execution shape (identical in spirit to the other adapters): the adapter never
imports pypsa itself. It writes ``config.json`` into the run dir and shells out
to ``adapters/pypsa_toy/runner.py`` under an interpreter that *does* have
pypsa/linopy/highspy (``PYPSA_PYTHON``), teeing the child's merged output into
``<run_dir>/solver.log`` line by line so the live monitor sees the solve as it
happens. The child prints a marker line

    [pypsa_toy] status: OPTIMAL | INFEASIBLE | TIME_LIMIT | ERROR

which :meth:`PypsaToyAdapter._parse_status` reads, with token fallbacks (the
HiGHS ``Model   status`` line, linopy's termination condition, a Python
traceback) for the case where the child dies before printing it.

Nothing here raises on an infeasible or failed solve — that is a *result* the
log-analyzer reads off the :class:`~framework.run_record.Execution`.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
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

from adapters.pypsa_toy import schema
from adapters.pypsa_toy.schema import (
    ADAPTER_NAME,
    ALLOWED_VALUES,
    DEFAULTS,
    KEY_SPECS,
    TIER_A_KEYS,
    TIER_B_KEYS,
    TIER_C_KEYS,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
RUNNER = Path(__file__).resolve().parent / "runner.py"

#: Interpreters that are likely to have pypsa, in preference order, when
#: ``PYPSA_PYTHON`` is unset. The miniforge base env is the documented one.
_CANDIDATE_PYTHONS = [
    Path.home() / "miniforge3" / "bin" / "python",
    Path.home() / "miniforge3" / "envs" / "agentic-pypsa" / "bin" / "python",
]

MARK = "[pypsa_toy]"

#: Canonical statuses this adapter can report, most-specific first.
STATUSES = ("OPTIMAL", "TIME_LIMIT", "INFEASIBLE", "ERROR")

#: Fallback tokens, scanned in order, when the marker line is missing. Each is
#: (lowercase token, status, error_origin).
_FALLBACK_TOKENS: List[Tuple[str, str, Optional[str]]] = [
    ("termination condition: infeasible", "INFEASIBLE", "solver"),
    ("model   status      : infeasible", "INFEASIBLE", "solver"),
    ("model status: infeasible", "INFEASIBLE", "solver"),
    ("problem status detected on presolve: infeasible", "INFEASIBLE", "solver"),
    ("infeasible or unbounded", "INFEASIBLE", "solver"),
    ("termination condition: time_limit", "TIME_LIMIT", None),
    ("time limit reached", "TIME_LIMIT", None),
    ("termination condition: optimal", "OPTIMAL", None),
    ("model   status      : optimal", "OPTIMAL", None),
    ("modulenotfounderror: no module named 'pypsa'", "ERROR", "preflight"),
    ("modulenotfounderror", "ERROR", "runtime"),
    ("traceback (most recent call last)", "ERROR", "runtime"),
]

#: True when Workstream D's monitor field exists on Execution.
_EXECUTION_HAS_MONITOR = any(f.name == "monitor" for f in fields(Execution))


def default_python() -> str:
    """The interpreter used to run ``runner.py``.

    ``PYPSA_PYTHON`` wins; otherwise the first existing candidate; otherwise
    the interpreter running the framework (which may lack pypsa — the runner
    then exits with a clear ``ModuleNotFoundError`` that
    :meth:`PypsaToyAdapter._parse_status` maps to ERROR/preflight)."""
    env = os.environ.get("PYPSA_PYTHON")
    if env:
        return env
    for candidate in _CANDIDATE_PYTHONS:
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return sys.executable


class PypsaToyAdapter(Adapter):
    """Adapter for the deterministic 3-bus pypsa toy model."""

    name = ADAPTER_NAME

    def __init__(
        self,
        python: Optional[str] = None,
        runner: str | Path = RUNNER,
        repo_root: str | Path = REPO_ROOT,
    ):
        self.python = python or default_python()
        self.runner = Path(runner).resolve()
        self.repo_root = Path(repo_root).resolve()

    # ---- validation ------------------------------------------------------

    def validate_config(self, config: Dict[str, Any]) -> ValidationResult:
        """Schema-only preflight: closed key set, types, ranges, enums, plus a
        check that the runner script and an interpreter exist. Cheap and
        side-effect free — it never builds a network or touches a solver."""
        errors = list(schema.validate(config))
        if not self.runner.is_file():
            errors.append(f"runner script not found at {self.runner}")
        if not (Path(self.python).is_file() or shutil.which(self.python)):
            errors.append(
                f"interpreter {self.python!r} not found; set PYPSA_PYTHON to a "
                "python that has pypsa, linopy and highspy installed"
            )
        return ValidationResult(ok=not errors, errors=errors)

    def check_interpreter(self) -> subprocess.CompletedProcess:
        """Opt-in deep preflight: does ``self.python`` actually import pypsa?
        Not called by :meth:`validate_config` (which must stay side-effect
        free); the smoke test and the scenario-builder skill may call it."""
        return subprocess.run(
            [self.python, "-c",
             "import pypsa, linopy, highspy; print(pypsa.__version__)"],
            capture_output=True, text=True,
        )

    def horizon_facts(self, config: Dict[str, Any]) -> Dict[str, float]:
        """Analytic facts about a config's horizon (demand energy, RE
        potential, the must-run emissions floor). Needs numpy but not pypsa, so
        the scenario-builder can tell in advance that e.g. ``co2_cap_t`` sits
        below the physical floor."""
        from adapters.pypsa_toy.network import horizon_summary

        return horizon_summary(config)

    # ---- execution -------------------------------------------------------

    def run(
        self,
        config: Dict[str, Any],
        run_dir: Path,
        on_event: Optional[Callable[..., Any]] = None,
        on_line: Optional[Callable[[str], None]] = None,
    ) -> Execution:
        """Execute one run; never raises on an infeasible/failed solve.

        Writes ``config.json``, streams ``runner.py`` into ``solver.log``, and
        returns an :class:`Execution`. ``on_line`` receives each output line as
        it arrives; ``on_event`` is the framework's live-monitoring hook (an
        event callback wrapped in a ``framework.monitor.RunMonitor``, or a
        ready-made monitor exposing ``on_line``). Monitoring never fails a run.
        """
        run_dir = Path(run_dir)
        run_dir.mkdir(parents=True, exist_ok=True)
        cfg_path = self._write_config(config, run_dir)
        log_path = run_dir / "solver.log"

        monitor = self._make_monitor(config, run_dir, on_event)
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

        result = self._stream(self._command(cfg_path, run_dir), log_path, sink, monitor)
        returncode = result.returncode
        if returncode is not None:
            returncode = int(returncode)
        wall = float(result.wall_seconds)
        text = "".join(lines)
        status, origin = self._parse_status(text, returncode)

        execution = Execution(
            termination_status=status,
            wall_seconds=round(wall, 2),
            solver_log="solver.log",
            returncode=returncode,
            error_origin=origin,
        )
        if monitor is not None:
            summary = self._finish_monitor(monitor, result)
            if summary is not None and _EXECUTION_HAS_MONITOR:
                execution.monitor = summary  # type: ignore[attr-defined]
        return execution

    # ---- framework contract ---------------------------------------------

    def intervention_spec(self) -> InterventionSpec:
        return InterventionSpec(
            # A — solver numerics: they change how HiGHS searches, not the LP.
            tier_a_keys=set(TIER_A_KEYS),
            # B — sanctioned parameters: prices, capex, demand scale, horizon,
            # network expansion. They change the answer but stay inside the
            # space the modeler sanctioned.
            tier_b_keys=set(TIER_B_KEYS),
            # C — the policy constraints. Relaxing the carbon cap, the RE-share
            # floor, or the "all demand must be served" rule changes what the
            # study claims, so the framework refuses to do it silently.
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

    def _command(self, cfg_path: Path, run_dir: Path) -> List[str]:
        return [self.python, str(self.runner),
                "--config", str(cfg_path), "--run-dir", str(run_dir)]

    def _env(self) -> Dict[str, str]:
        """The child must import ``adapters.pypsa_toy`` from this repo even
        when the framework is not installed, so put the repo root on
        PYTHONPATH. ``PYTHONUNBUFFERED`` keeps the tee line-accurate."""
        env = dict(os.environ)
        existing = env.get("PYTHONPATH", "")
        parts = [str(self.repo_root)] + ([existing] if existing else [])
        env["PYTHONPATH"] = os.pathsep.join(parts)
        env["PYTHONUNBUFFERED"] = "1"
        return env

    @staticmethod
    def _write_config(config: Dict[str, Any], run_dir: Path) -> Path:
        run_dir = Path(run_dir)
        run_dir.mkdir(parents=True, exist_ok=True)
        cfg_path = run_dir / "config.json"
        cfg_path.write_text(json.dumps(config, indent=2, sort_keys=True))
        return cfg_path

    def _stream(self, cmd: Sequence[str], log_path: Path,
                on_line: Callable[[str], None], monitor: Any = None) -> Any:
        """Run ``cmd``, tee merged stdout/stderr into ``log_path`` line by line,
        and return the stream result (``returncode`` / ``wall_seconds``, plus
        ``timed_out`` / ``aborted`` / ``abort_reason`` on the framework path).

        With a ``monitor``, ``framework.process.stream_command`` also drives
        ``monitor.tick()`` while the solver is quiet (wall-clock stall and
        time-limit-near detection) and honours ``monitor.abort_event``.
        ``stream_local`` is the fallback for a checkout without
        ``framework/process.py``.
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
            return stream_command(list(cmd), cwd=str(self.repo_root),
                                  log_path=str(log_path), env=self._env(),
                                  on_line=on_line, **kwargs)
        returncode, wall = stream_local(cmd, cwd=self.repo_root, log_path=log_path,
                                        env=self._env(), on_line=on_line)
        return SimpleNamespace(returncode=returncode, wall_seconds=wall,
                               timed_out=False, aborted=False, abort_reason=None)

    @staticmethod
    def _make_monitor(config: Dict[str, Any], run_dir: Path, on_event: Any) -> Any:
        """Wrap ``on_event`` in a live ``RunMonitor`` when Workstream D's
        module is importable. ``on_event`` may already *be* a monitor."""
        if on_event is None:
            return None
        if callable(getattr(on_event, "on_line", None)):
            return on_event
        try:
            from framework.monitor import RunMonitor  # type: ignore
        except ImportError:
            return None
        try:
            return RunMonitor(run_dir, on_event=on_event,
                              time_limit=config.get("time_limit", DEFAULTS["time_limit"]))
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

    @staticmethod
    def _parse_status(output: str, returncode: Optional[int]) -> Tuple[str, Optional[str]]:
        """Map the runner's output (+ exit code) to (status, error_origin).

        The authority is the runner's own ``[pypsa_toy] status: X`` marker,
        paired with its ``[pypsa_toy] error_origin: Y`` line. Token scanning is
        only a fallback for a child that died before printing the marker (an
        interpreter without pypsa, a kill, a crash in pypsa itself).
        """
        marker_status: Optional[str] = None
        marker_origin: Optional[str] = None
        for raw in output.splitlines():
            line = raw.strip()
            if not line.startswith(MARK):
                continue
            body = line[len(MARK):].strip()
            if body.startswith("status:"):
                value = body.split(":", 1)[1].strip().upper()
                if value in STATUSES:
                    marker_status = value
            elif body.startswith("error_origin:"):
                marker_origin = body.split(":", 1)[1].strip() or None

        if marker_status is not None:
            origin = marker_origin
            if marker_status != "ERROR" and origin not in ("solver", None):
                origin = None
            if marker_status == "ERROR" and origin is None:
                origin = "runtime"
            if marker_status in ("OPTIMAL", "TIME_LIMIT"):
                origin = None
            return marker_status, origin

        low = output.lower()
        for token, status, origin in _FALLBACK_TOKENS:
            if token in low:
                if status == "OPTIMAL" and returncode not in (0, None):
                    # It said optimal but then died — that is a runtime error.
                    return "ERROR", "runtime"
                return status, origin

        if returncode in (0, None):
            # Exited cleanly but said nothing we recognise: still not a success.
            return "ERROR", "runtime"
        return "ERROR", "runtime"


# ---- local tee (used when framework.process is unavailable) -----------------

def stream_local(cmd: Sequence[str], cwd: Path, log_path: Path,
                 env: Optional[Dict[str, str]] = None,
                 on_line: Optional[Callable[[str], None]] = None) -> Tuple[int, float]:
    """Minimal ``stream_command`` stand-in: Popen + line-by-line tee."""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    start = time.monotonic()
    with log_path.open("w", encoding="utf-8", errors="replace") as fh:
        proc = subprocess.Popen(
            [str(c) for c in cmd], cwd=str(cwd), env=env,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1, encoding="utf-8", errors="replace",
        )
        assert proc.stdout is not None
        for line in iter(proc.stdout.readline, ""):
            fh.write(line)
            fh.flush()
            if on_line is not None:
                try:
                    on_line(line)
                except Exception:
                    pass
        proc.stdout.close()
        returncode = proc.wait()
    return int(returncode), time.monotonic() - start


# ---- describe_config --------------------------------------------------------

_TIER_BLURB = {
    "A": "Tier A (numerics — the agent may change these on its own)",
    "B": "Tier B (sanctioned parameters — auto-applied but always flagged)",
    "C": "Tier C (POLICY — never auto-applied; needs human sign-off)",
}


def _fmt_range(spec: schema.KeySpec) -> str:
    bits = []
    if spec.enum is not None and spec.key != "snapshots_days":
        bits.append("one of " + ", ".join(json.dumps(v) for v in spec.enum))
    if spec.minimum is not None:
        bits.append(("> " if spec.exclusive_minimum else ">= ") + str(spec.minimum))
    if spec.maximum is not None:
        bits.append("<= " + str(spec.maximum))
    return "; ".join(bits) if bits else "any value of its type"


def describe_schema() -> str:
    """The full human-readable model description handed to the LLM-facing
    skills. It must be enough to build a legal config, understand what each
    key *means physically*, and know which changes are policy relaxations."""
    lines: List[str] = []
    add = lines.append

    add("MODEL: pypsa_toy — a deterministic 3-bus capacity-expansion LP (PyPSA + HiGHS).")
    add("")
    add("It is a toy, but a complete one: hourly snapshots, investment and dispatch in")
    add("one LP, a transmission network, storage, a carbon cap and a renewable-share")
    add("floor. It needs no licence and no external data — the demand/wind/solar")
    add("profiles are generated from a fixed seed, so the same config always yields")
    add("the same LP and the same answer, on any machine.")
    add("")
    add("TOPOLOGY (a triangle of three buses):")
    add("  north — 500 MW peak demand. Hosts coal_existing (300 MW, MUST-RUN at 20 %,")
    add("          38 % efficient), coal_new (extendable to 1000 MW, 42 %), and gas")
    add("          (extendable without limit, 55 % CCGT).")
    add("  east  — 300 MW peak. Wind, extendable to 600 MW (46.5 % mean capacity")
    add("          factor over the 14-day series, so ~27 $/MWh at the default capex),")
    add("          plus a 4 h battery extendable to 500 MW.")
    add("  south — 200 MW peak. Solar, extendable to 800 MW (15.8 % mean capacity")
    add("          factor, so ~58 $/MWh at the default capex),")
    add("          plus a 4 h battery extendable to 500 MW.")
    add("  Lines: north-east 400 MW, north-south 400 MW, east-south 200 MW; extendable")
    add("          to 20 GW at 20,000 $/MW/yr when line_expansion_allowed is true.")
    add("  Emission factors: coal 0.34, gas 0.20 t CO2 per MWh of *thermal* input, so")
    add("          the electrical intensity is factor/efficiency: coal_existing ~0.89")
    add("          t/MWh_el, coal_new ~0.81, gas ~0.36.")
    add("")
    add("HORIZON: `snapshots_days` x 24 hourly snapshots starting 2030-01-07 (a Monday);")
    add("14 days = 336 h is the default. Annualised capital costs are pro-rated to the")
    add("horizon, so build decisions stay economically sane at any horizon length. Day k")
    add("of a short horizon is exactly day k of the 14-day series, so a 1-day run is a")
    add("strict subset of the full one — use snapshots_days=1 for fast iteration.")
    add("")
    add("CONFIG SCHEMA — a flat JSON object. The key set is CLOSED: an unknown key is a")
    add("validation error, not a silently-ignored typo. Every key has a default, so the")
    add("empty object {} is a legal (and feasible) config.")
    add("")
    for tier in ("C", "B", "A"):
        add(_TIER_BLURB[tier] + ":")
        for spec in KEY_SPECS:
            if spec.tier != tier:
                continue
            add(f"  - {spec.key} ({spec.type}, default {json.dumps(spec.default)}; "
                f"{_fmt_range(spec)})")
            for chunk in _wrap(spec.description, 74):
                add("      " + chunk)
        add("")

    add("PHYSICAL LIMITS THAT DECIDE FEASIBILITY (check these before proposing a fix):")
    add("  - The must-run coal unit emits ~1,289 t CO2 per modelled day whatever else")
    add("    happens (60 MW_el / 0.38 x 0.34 x 24 h). A co2_cap_t below")
    add("    snapshots_days x 1,289 t is unreachable by ANY Tier-A or Tier-B change.")
    add("  - Wind (<=600 MW) and solar (<=800 MW) technical potential sums to ~56 % of")
    add("    default demand over 14 days (~50 % over 1 day, ~65 % over 7). Above that,")
    add("    a re_share_min is unreachable however cheap the capex keys are made; and")
    add("    the must-run coal unit puts an absolute ceiling near 0.92 at any demand.")
    add("  - With line_expansion_allowed = false the east and south buses can import at")
    add("    most 600 and 600 MW respectively, so demand_scale beyond ~4 is infeasible")
    add("    unless shedding is allowed.")
    add("  - With allow_load_shedding = false EVERY MWh of demand must be served. That")
    add("    is the study's premise, not a numerical detail: turning it on to escape an")
    add("    infeasibility answers a different question.")
    add("  The runner prints all of these as `[pypsa_toy] horizon <key> = <value>` lines")
    add("  at the top of solver.log, computed for the actual config.")
    add("")
    add("EXECUTION: the adapter runs `adapters/pypsa_toy/runner.py` under PYPSA_PYTHON")
    add("(an interpreter with pypsa, linopy and highspy). The child prints")
    add("`[pypsa_toy] status: OPTIMAL|INFEASIBLE|TIME_LIMIT|ERROR` and, on a failure,")
    add("`[pypsa_toy] error_origin: preflight|solver|runtime`, alongside the raw HiGHS")
    add("log. A 14-day solve takes ~1-3 s; a 1-day solve well under a second — so a")
    add("TIME_LIMIT here only ever comes from an absurdly small time_limit.")
    add("")
    add("OUTPUTS (CSV, archived in <run_dir>/outputs/):")
    add("  generator_results  — per generator: p_nom_opt_mw, energy_mwh, energy_share,")
    add("                       capacity_factor, curtailment_mwh, costs, emissions_t")
    add("  storage_results    — per battery: p_nom_opt_mw, energy_capacity_mwh, cycles")
    add("  line_results       — per line: s_nom_opt_mw, expansion_mw, utilisation_max,")
    add("                       congested_hours")
    add("  cost_results       — one row: objective_usd, capital/variable/shedding cost,")
    add("                       total_system_cost_usd, average_cost_usd_per_mwh,")
    add("                       emissions_t, re_share_of_generation, shed_share_of_demand")
    add("  emissions_results  — per carrier + total: energy_mwh, emissions_t, intensity,")
    add("                       cap_utilisation, cap_shadow_price_usd_per_t")
    add("  nse_results        — per bus + total: demand_mwh, shed_mwh, shed_share,")
    add("                       shed_hours")
    add("")
    add("SANITY ANCHORS for the output analyzer (defaults, 14 days):")
    add("  - re_share_of_generation is normally ~0.2-0.5. Near 0 means the RE capex")
    add("    keys were set absurdly high, or a fuel price was set to 0; near 1 is")
    add("    impossible with must-run coal.")
    add("  - shed_share_of_demand must be 0.0 whenever allow_load_shedding is false.")
    add("  - gas_price = 0 makes gas free: gas_energy_share jumps to ~1 and")
    add("    average_cost_usd_per_mwh collapses. That is a planted anomaly, not a")
    add("    discovery — the model warns about it in the log.")
    add("  - cap_shadow_price_usd_per_t >> 0 means the carbon cap is binding; report it")
    add("    rather than relaxing the cap.")
    return "\n".join(lines)


def _wrap(text: str, width: int) -> List[str]:
    words = text.split()
    out: List[str] = []
    cur = ""
    for word in words:
        if cur and len(cur) + 1 + len(word) > width:
            out.append(cur)
            cur = word
        else:
            cur = f"{cur} {word}".strip()
    if cur:
        out.append(cur)
    return out
