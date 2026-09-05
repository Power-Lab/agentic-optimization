"""Live run monitoring: solver/model log lines -> structured events.

"Analyze while it runs." An adapter streams its subprocess output through a
:class:`RunMonitor`; the monitor turns each line into zero or more
:class:`RunEvent` objects, keeps a rolling summary (best objective, bound,
gap, phase, warnings, stall flag ...), fans events out to an ``on_event``
callback, and persists ``monitor.json`` in the run dir so a human or an agent
can look at a solve that is still in progress (see ``python -m framework.watch``).

Model-agnostic by construction: the parser understands the log *dialects* the
adapters stream — the HiGHS and Gurobi solver logs, the generic status/stage
marker lines a model wrapper prints (``... solved successfully``,
``... is infeasible``, ``status: INFEASIBLE``), Julia and Python tracebacks,
and WARNING lines. Nothing here knows any model's filenames or config keys.

Two layers:

* :class:`LogWatcher` — a pure line-in / events-out state machine. Feed it
  lines (live, or by replaying a saved ``solver.log``) and it yields events.
  Also does stall and time-limit-near detection.
* :class:`RunMonitor` — wraps a watcher for an adapter: rolling summary,
  ``on_event`` fan-out, an abort flag, and ``monitor.json`` persistence.

Event kinds (``RunEvent.kind``) — see :data:`EVENT_KINDS` for the list and
the run-monitor skill for what to do about each.
"""

from __future__ import annotations

import json
import math
import os
import re
import threading
import time
from collections import Counter, deque
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Deque, Dict, Iterable, List, Optional

SCHEMA_VERSION = 1

#: Every event kind the watcher can emit, with the keys found in ``data``.
EVENT_KINDS = (
    "solver_start",     # solver banner seen: solver, version
    "model_size",       # rows, cols, nonzeros (+ sense / integer counts when printed)
    "phase",            # solve phase changed: phase, previous, source
    "progress",         # MIP tree row: incumbent, bound, gap, nodes, time_s, improved
    "incumbent",        # a MIP row / line carrying a NEW incumbent (same data as progress)
    "barrier_iter",     # barrier iteration: iter, objective (primal), dual, time_s
    "simplex_iter",     # simplex iteration: iter, objective, time_s
    "root_relaxation",  # root LP relaxation of a MIP done: objective, iterations, time_s
    "solution",         # final objective / bound / gap lines: objective, bound, gap, final
    "explored",         # solver's end-of-search summary: nodes, iterations, time_s
    "solver_status",    # the solver's own verdict: status (normalised), raw
    "status",           # a model-wrapper status marker line: status (normalised), raw
    "preflight",        # preflight passed / failed marker: passed
    "warning",          # a WARNING line: text
    "error",            # an ERROR line that is not (yet) a traceback: text, reason
    "traceback",        # Julia / Python traceback: language, message, frames
    "stall",            # no improvement / no output for stall_seconds: reason, idle_s
    "time_limit_near",  # elapsed >= fraction * time_limit: elapsed_s, time_limit, fraction
)

PHASES = (
    "starting", "preflight", "presolve", "root_relaxation", "barrier", "crossover",
    "simplex", "branch_and_bound", "finished", "error",
)

#: Normalised statuses that end a solve. Anything else is informational.
TERMINAL_STATUSES = frozenset(
    {"OPTIMAL", "INFEASIBLE", "UNBOUNDED", "TIME_LIMIT", "LIMIT", "INTERRUPTED", "ERROR"}
)

#: Event kinds worth persisting / printing immediately (vs. bulk progress rows).
NOTABLE_KINDS = frozenset(
    {"solver_start", "model_size", "phase", "incumbent", "root_relaxation", "solution",
     "explored", "solver_status", "status", "preflight", "warning", "error", "traceback",
     "stall", "time_limit_near"}
)


@dataclass
class RunEvent:
    """One thing the monitor noticed in the log stream."""

    kind: str
    wall_s: float              # seconds since the watcher started
    message: str               # the raw log line (stripped) or a human sentence
    data: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    def __str__(self) -> str:  # compact one-liner for tails / CLIs
        bits = []
        for key in ("status", "phase", "incumbent", "objective", "bound", "gap_pct", "nodes",
                    "time_s", "reason", "idle_s", "language", "solver", "rows", "cols"):
            if key in self.data and self.data[key] is not None:
                val = self.data[key]
                if isinstance(val, float):
                    val = f"{val:.6g}"
                bits.append(f"{key}={val}")
        detail = " ".join(bits)
        if self.kind in ("warning", "error", "traceback", "status", "solver_status", "preflight"):
            detail = (detail + " " if detail else "") + self.message[:120]
        return f"[{self.wall_s:8.1f}s] {self.kind:<15} {detail}"


# --------------------------------------------------------------------------
# Regular expressions for the log dialects
# --------------------------------------------------------------------------

_F = r"[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?"       # a float
_NUM_OR_DASH = rf"(?:{_F}|-)"
_NUM_OR_INF = rf"(?:{_F}|-?inf)"

# --- Gurobi -----------------------------------------------------------------
_GRB_BANNER = re.compile(r"^Gurobi Optimizer version (\S+)")
_GRB_MODEL = re.compile(
    r"^Optimize a model with (\d+) rows, (\d+) columns and (\d+) nonzeros(?: \((Min|Max)\))?"
)
_GRB_VARTYPES = re.compile(r"^Variable types: (\d+) continuous, (\d+) integer \((\d+) binary\)")
_GRB_PRESOLVED = re.compile(r"^Presolved: (\d+) rows, (\d+) columns, (\d+) nonzeros")
_GRB_PRESOLVE = re.compile(r"^Presolve (?:removed|time|added)|^Presolved:")
_GRB_TIMELIMIT = re.compile(r"^(?:Set parameter TimeLimit to value\s+|TimeLimit\s+)(\S+)")
_GRB_HEURISTIC = re.compile(rf"^Found heuristic solution: objective ({_F})")
_GRB_BARRIER_HDR = re.compile(r"^Root barrier log|^Barrier statistics|^Barrier performed")
_GRB_BARRIER_ROW = re.compile(
    rf"^\s*(\d+)\s+({_F})\s+({_F})\s+({_F})\s+({_F})\s+({_F})\s+(\d+)s$"
)
_GRB_BARRIER_DONE = re.compile(r"^Barrier solved model in (\d+) iterations and ([\d.]+) seconds")
_GRB_OPT_OBJ = re.compile(rf"^Optimal objective\s+({_F})")
_GRB_CROSSOVER = re.compile(r"^Root crossover log|^Crossover time|^Use crossover")
_GRB_SIMPLEX_HDR = re.compile(r"^Root simplex log|^Iteration\s+Objective\s+Primal Inf")
_GRB_SIMPLEX_ROW = re.compile(rf"^\s*(\d+)\s+({_F})\s+({_F})\s+({_F})\s+(\d+)s$")
_GRB_ROOT_RELAX = re.compile(
    rf"^Root relaxation: objective ({_F}), (\d+) iterations, ([\d.]+) seconds"
)
_GRB_MIP_HDR = re.compile(r"^\s*Nodes\s+\|\s+Current Node")
_GRB_MIP_ROW = re.compile(
    rf"^(?P<flag>[H*])?\s*(?P<expl>\d+)\s+(?P<unexpl>\d+)"
    rf"(?:\s+(?P<mid>\S.*?))?\s+(?P<inc>{_NUM_OR_DASH})\s+(?P<bound>{_F})"
    rf"\s+(?P<gap>\d+(?:\.\d+)?%|-)\s+(?P<itnode>{_NUM_OR_DASH})\s+(?P<time>\d+)s$"
)
_GRB_EXPLORED = re.compile(
    r"^Explored (\d+) nodes \((\d+) simplex iterations\) in ([\d.]+) seconds"
)
_GRB_FINAL_OPT = re.compile(r"^Optimal solution found(?: \(tolerance ([\d.eE+-]+)\))?")
_GRB_BEST = re.compile(
    rf"^Best objective ({_NUM_OR_DASH}), best bound ({_NUM_OR_DASH}), gap ({_F}%|-)"
)
_GRB_SOLVED_IN = re.compile(r"^Solved in (\d+) iterations and ([\d.]+) seconds")
_GRB_STATUS = (
    (re.compile(r"^Model is infeasible or unbounded"), "INFEASIBLE"),
    (re.compile(r"^Model is infeasible"), "INFEASIBLE"),
    (re.compile(r"^Infeasible model"), "INFEASIBLE"),
    (re.compile(r"^Infeasible or unbounded model"), "INFEASIBLE"),
    (re.compile(r"^Model is unbounded"), "UNBOUNDED"),
    (re.compile(r"^Unbounded model"), "UNBOUNDED"),
    (re.compile(r"^Time limit reached"), "TIME_LIMIT"),
    (re.compile(r"^(?:Node|Solution|Iteration|Work|Memory) limit reached"), "LIMIT"),
    (re.compile(r"^Solve interrupted"), "INTERRUPTED"),
    (re.compile(r"^Numerical trouble encountered|^Numeric error|^Out of memory|"
                r"^Sub-optimal termination"), "ERROR"),
)

# --- HiGHS ------------------------------------------------------------------
_HI_BANNER = re.compile(r"^Running HiGHS (\S+)")
_HI_MIP = re.compile(r"^Solving MIP model with:")
_HI_HAS = re.compile(
    r"^(MIP|LP|QP|MIQP) has (\d+) rows?; (\d+) cols?; (\d+) nonzeros?"
    r"(?:; (\d+) integer variables? \((\d+) binary\))?"
)
_HI_SIZE_PROGRESS = re.compile(r"^\s*(\d+) rows, (\d+) cols, (\d+) nonzeros")
_HI_SIZE_ROWS = re.compile(r"^\s*(\d+) rows$")
_HI_SIZE_COLS = re.compile(
    r"^\s*(\d+) cols(?: \((\d+) binary, (\d+) integer, (\d+) implied int\., (\d+) continuous"
    r"(?:, (\d+) domain fixed)?\))?$"
)
_HI_SIZE_NZ = re.compile(r"^\s*(\d+) nonzeros$")
_HI_PRESOLVE = re.compile(r"^Presolving model|^Presolve\s*:\s*Reductions|^Presolve reductions:")
_HI_PRESOLVE_INF = re.compile(r"^Presolve\s*:\s*(?:Primal )?[Ii]nfeasible")
_HI_PRESOLVE_UNB = re.compile(r"^Presolve\s*:\s*[Uu]nbounded")
# "Problem status detected on presolve: Infeasible" (HiGHS LP path)
_HI_PRESOLVE_STATUS = re.compile(r"^Problem status detected on presolve\s*:\s*(.+?)\s*$")
_HI_LP = re.compile(
    r"^Solving the presolved LP|^Solving LP without presolve|^Solving the original LP|"
    r"^Using (?:EKK )?(?:dual|primal) simplex"
)
_HI_IPM = re.compile(r"^Solving LP with IPM|^\s*IPX version|^Using IPX")
_HI_MIP_HDR = re.compile(r"^\s*(?:Src\s+)?Proc\. InQueue\s*\|")
_HI_SRC_LEGEND = re.compile(r"^\s*(?:Src:\s*)?[A-Za-z] => [A-Za-z]")
_HI_COUNT = r"\d+(?:\.\d+)?[kMG]?"
_HI_MIP_ROW = re.compile(
    rf"^\s*(?:(?P<flag>[A-Za-z])\s+)?(?P<proc>\d+)\s+(?P<inq>\d+)\s+(?P<leaves>\d+)"
    rf"\s+(?P<expl>\d+(?:\.\d+)?)%\s+(?P<bound>{_NUM_OR_INF})\s+(?P<sol>{_NUM_OR_INF})"
    rf"\s+(?P<gap>\d+(?:\.\d+)?%|inf|Large)\s+(?P<cuts>{_HI_COUNT})\s+(?P<inlp>{_HI_COUNT})"
    rf"(?:\s+(?P<confl>{_HI_COUNT}))?\s+(?P<lpiters>{_HI_COUNT})\s+(?P<time>\d+(?:\.\d+)?)s$"
)
# "      48090     3.4655074496e-02 Pr: 1388(61986.2); Du: 0(2.16e-12) 3.9s" — the
# infeasibility columns (Pr / Du / Ph1) are optional: HiGHS drops them once zero.
_HI_SIMPLEX_ROW = re.compile(
    rf"^\s*(?P<iter>\d+)\s+(?P<obj>{_F})"
    rf"(?P<infeas>(?:;?\s+(?:Pr|Du|Ph1):\s*\d+\({_F}\))*)\s+(?P<time>\d+(?:\.\d+)?)s$"
)
_HI_INFEAS = re.compile(rf"(Pr|Du|Ph1):\s*(\d+)\(({_F})\)")
_HI_MODEL_STATUS = re.compile(r"^Model\s+status\s*:\s*(.+?)\s*$")
_HI_OBJ = re.compile(rf"^Objective value\s*:\s*({_F})")
_HI_REPORT = re.compile(r"^Solving report")
_HI_REPORT_STATUS = re.compile(r"^\s{1,6}Status\s+(\S.*?)\s*$")
_HI_REPORT_PRIMAL = re.compile(rf"^\s{{1,6}}Primal bound\s+({_NUM_OR_INF})")
_HI_REPORT_DUAL = re.compile(rf"^\s{{1,6}}Dual bound\s+({_NUM_OR_INF})")
_HI_REPORT_GAP = re.compile(rf"^\s{{1,6}}Gap\s+({_F}%|inf|Large)")
_HI_REPORT_TIMING = re.compile(r"^\s{1,6}Timing\s+([\d.]+)")
_HI_REPORT_NODES = re.compile(r"^\s{1,6}Nodes\s+(\d+)")
_HI_RUNTIME = re.compile(r"^HiGHS run time\s*:\s*([\d.]+)")

# --- generic wrapper / runtime lines ------------------------------------------
# "status: INFEASIBLE", "Termination status: TIME_LIMIT", "Termination condition: optimal"
_STATUS_TOKEN = re.compile(
    r"\b(?:status|termination condition)\s*[:=]\s*([A-Za-z][A-Za-z_]*)", re.I
)
_ORIGIN_TOKEN = re.compile(r"\berror_origin\s*[:=]\s*([A-Za-z_]+)", re.I)
_STAGE_TOKEN = re.compile(r"\bstage\s*[:=]\s*(\S+)", re.I)
_GENERIC_STATUS = re.compile(
    r"solved successfully|infeasible|unbounded|time limit|did not solve|status\s*[:=]|"
    r"termination condition\s*[:=]", re.I
)
_NEGATED_SUCCESS = re.compile(r"\bnot\b.*solved successfully", re.I)
_PREFLIGHT = re.compile(r"preflight", re.I)
_PREFLIGHT_OK = re.compile(r"passed|succeeded|\bok\b", re.I)
_PREFLIGHT_BAD = re.compile(r"fail|error|missing|not found|invalid", re.I)
_WARNING = re.compile(r"^(?:\W*|\[[^\]]*\]\s*)warning\b", re.I)
_ERROR = re.compile(r"^(?:\W*|\[[^\]]*\]\s*)error\b", re.I)
_LICENSE = re.compile(r"licen[cs]e", re.I)
_LICENSE_BAD = re.compile(
    r"\bexpired\b|not found|\bno (?:\w+ )?licen[cs]e\b|invalid|failed|denied|\berror\b|unable",
    re.I,
)
_PY_TB_START = re.compile(r"^Traceback \(most recent call last\):")
_PY_FRAME = re.compile(r'^\s+File "(.+?)", line (\d+), in (.+)$')
_PY_CHAIN = re.compile(
    r"^(?:During handling of the above exception|The above exception was the direct cause)"
)
_JL_ERROR = re.compile(r"^ERROR: (.*)$")
_JL_FRAME = re.compile(r"^\s*\[(\d+)\]\s+(.*)$")
_JL_LOC = re.compile(r"^\s+@\s+(.*)$")
_JL_EXPR = re.compile(r"^in expression starting at (.+)$")

_MAX_FRAMES = 15


# --------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------

def _num(text: Optional[str]) -> Optional[float]:
    if text is None:
        return None
    t = text.strip()
    if t in ("-", "", "inf", "-inf", "+inf"):
        return None
    try:
        return float(t)
    except ValueError:
        return None


def _pct(text: Optional[str]) -> Optional[float]:
    """'0.54%' -> 0.0054 ; '-' / 'inf' / 'Large' -> None."""
    if text is None:
        return None
    t = text.strip()
    if not t.endswith("%"):
        return None
    try:
        return float(t[:-1]) / 100.0
    except ValueError:
        return None


def _close(a: Optional[float], b: Optional[float]) -> bool:
    if a is None and b is None:
        return True
    if a is None or b is None:
        return False
    return math.isclose(a, b, rel_tol=1e-9, abs_tol=1e-12)


def normalize_status(token: str) -> Optional[str]:
    """Map a solver / wrapper status token onto the framework vocabulary.

    OPTIMAL | INFEASIBLE | UNBOUNDED | TIME_LIMIT | LIMIT | INTERRUPTED | ERROR,
    or the upper-cased token itself when it is something else (informational).
    """
    t = token.strip().strip(".").strip().upper().replace(" ", "_")
    if not t:
        return None
    if t in ("OPTIMAL", "LOCALLY_SOLVED", "SOLVED", "OPTIMAL_SOLUTION_FOUND"):
        return "OPTIMAL"
    if "INFEASIBLE" in t:
        return "INFEASIBLE"
    if "UNBOUNDED" in t:
        return "UNBOUNDED"
    if "TIME_LIMIT" in t:
        return "TIME_LIMIT"
    if "LIMIT" in t:
        return "LIMIT"
    if "INTERRUPT" in t:
        return "INTERRUPTED"
    if "ERROR" in t or "NUMERIC" in t or "FAIL" in t or t in ("UNKNOWN", "NOT_SET", "MEMORY_LIMIT"):
        return "ERROR"
    return t


def status_from_marker(line: str) -> Optional[str]:
    """Terminal status carried by a generic wrapper line, or None.

    Recognises ``... solved successfully``, ``... is infeasible``, ``... reached
    the time limit``, ``... did not solve``, and ``status: <TOKEN>``. Only a
    *terminal* status (see :data:`TERMINAL_STATUSES`) is returned: an
    informational token such as ``status: ok`` must never overwrite the
    solver's own verdict.
    """
    m = _STATUS_TOKEN.search(line)
    token_status = normalize_status(m.group(1)) if m else None
    if token_status in TERMINAL_STATUSES:
        return token_status
    low = line.lower()
    if "solved successfully" in low and not _NEGATED_SUCCESS.search(line):
        return "OPTIMAL"
    if "time limit" in low:
        return "TIME_LIMIT"
    if "infeasible" in low:
        return "INFEASIBLE"
    if "unbounded" in low:
        return "UNBOUNDED"
    if "did not solve" in low:
        return "ERROR"
    return None


# --------------------------------------------------------------------------
# LogWatcher
# --------------------------------------------------------------------------

class LogWatcher:
    """Line-in / events-out parser with stall and time-limit-near detection.

    Parameters
    ----------
    stall_seconds:
        Emit a ``stall`` event when neither the incumbent nor the bound has
        improved for this long (solver time when the log prints it, otherwise
        wall time), or when :meth:`tick` sees no output at all for this long.
        ``None`` disables stall detection.
    time_limit:
        The solver time limit in seconds, if known (adapters usually pass the
        config's value). Gurobi logs also print it and the watcher learns it
        from there. A ``time_limit_near`` event fires once when the solve is
        past ``time_limit_fraction`` of it.
    clock:
        Monotonic-seconds callable, injectable for tests.
    """

    def __init__(
        self,
        stall_seconds: Optional[float] = 600.0,
        time_limit: Optional[float] = None,
        time_limit_fraction: float = 0.9,
        clock: Optional[Callable[[], float]] = None,
        keep: int = 50,
    ) -> None:
        self._clock = clock or time.monotonic
        self._t0 = self._clock()
        self.stall_seconds = stall_seconds
        self.time_limit = float(time_limit) if time_limit else None
        self.time_limit_fraction = time_limit_fraction
        self.keep = keep

        # rolling public state
        self.solver: Optional[str] = None
        self.solver_version: Optional[str] = None
        self.is_mip = False
        self.phase = "starting"
        self.status: Optional[str] = None
        self.status_source: Optional[str] = None
        self.error_origin: Optional[str] = None
        self.best_obj: Optional[float] = None
        self.bound: Optional[float] = None
        self.gap: Optional[float] = None
        self.nodes: Optional[int] = None
        self.lp_objective: Optional[float] = None
        self.model_size: Dict[str, Any] = {}
        self.solver_time_s: Optional[float] = None
        self.n_lines = 0
        self.n_incumbents = 0
        self.n_warnings = 0
        self.n_errors = 0
        self.warnings: Deque[str] = deque(maxlen=keep)
        self.errors: Deque[str] = deque(maxlen=keep)
        self.traceback: Optional[Dict[str, Any]] = None
        self.n_stalls = 0
        self.stall_reason: Optional[str] = None
        self.time_limit_near = False
        self.finished = False
        self.last_line_wall_s: Optional[float] = None
        self.last_progress_wall_s: Optional[float] = None

        # private state
        self._last_improve_wall = self._t0
        self._last_improve_solver_t: Optional[float] = None
        self._stalled_improve = False
        self._stalled_output = False
        self._in_report = False
        self._hi_size: Dict[str, Any] = {}
        self._py_tb: Optional[Dict[str, Any]] = None
        self._jl_tb: Optional[Dict[str, Any]] = None
        self._jl_collecting = False

    # ---- public --------------------------------------------------------

    @property
    def stalled(self) -> bool:
        return self._stalled_improve or self._stalled_output

    @property
    def elapsed_s(self) -> float:
        return self._clock() - self._t0

    def feed(self, line: str) -> List[RunEvent]:
        """Consume one log line; return the events it produced (often none)."""
        line = line.rstrip("\r\n")
        now = self._clock()
        self.n_lines += 1
        self.last_line_wall_s = now
        self._stalled_output = False
        stripped = line.strip()
        events: List[RunEvent] = []

        if self._py_tb is not None:
            ev = self._feed_py_tb(line, stripped, now)
            if ev is not None:
                events.append(ev)
            return events

        if self._jl_tb is not None:
            consumed, ev = self._feed_jl_tb(line, stripped, now)
            if ev is not None:
                events.append(ev)
            if consumed:
                return events

        events.extend(self._parse(line, stripped, now))
        events.extend(self._time_checks(now))
        return events

    def feed_many(self, lines: Iterable[str]) -> List[RunEvent]:
        out: List[RunEvent] = []
        for line in lines:
            out.extend(self.feed(line))
        return out

    def tick(self) -> List[RunEvent]:
        """Time-based checks with no new line (call every second or so)."""
        now = self._clock()
        events: List[RunEvent] = []
        if (self.stall_seconds is not None and not self.finished
                and not self._stalled_output):
            last = self.last_line_wall_s if self.last_line_wall_s is not None else self._t0
            idle = now - last
            if idle >= self.stall_seconds:
                self._stalled_output = True
                self.n_stalls += 1
                self.stall_reason = "no_output"
                events.append(self._event(
                    "stall", now, f"no solver output for {idle:.0f}s",
                    {"reason": "no_output", "idle_s": round(idle, 1), "phase": self.phase,
                     "gap": self.gap, "incumbent": self.best_obj, "bound": self.bound},
                ))
        events.extend(self._time_checks(now))
        return events

    def finish(self) -> List[RunEvent]:
        """Flush pending state (an unterminated traceback) at end of stream."""
        now = self._clock()
        events: List[RunEvent] = []
        if self._py_tb is not None:
            tb = self._py_tb
            self._py_tb = None
            tb["message"] = tb.get("message") or "traceback (truncated)"
            events.append(self._finalise_tb("python", tb, now, tb["message"]))
        if self._jl_tb is not None:
            tb = self._jl_tb
            self._jl_tb = None
            if tb.get("frames"):
                events.append(self._finalise_tb("julia", tb, now, tb["message"]))
        self.finished = True
        return events

    def snapshot(self) -> Dict[str, Any]:
        """The rolling summary as a plain dict (what RunMonitor persists)."""
        return {
            "solver": self.solver,
            "solver_version": self.solver_version,
            "is_mip": self.is_mip,
            "phase": self.phase,
            "status": self.status,
            "status_source": self.status_source,
            "error_origin": self.error_origin,
            "best_obj": self.best_obj,
            "bound": self.bound,
            "last_gap": self.gap,
            "gap_pct": None if self.gap is None else round(self.gap * 100.0, 4),
            "nodes": self.nodes,
            "lp_objective": self.lp_objective,
            "model_size": dict(self.model_size),
            "n_lines": self.n_lines,
            "n_incumbents": self.n_incumbents,
            "n_warnings": self.n_warnings,
            "warnings": list(self.warnings),
            "n_errors": self.n_errors,
            "errors": list(self.errors),
            "traceback": self.traceback,
            "stalled": self.stalled,
            "stall_reason": self.stall_reason,
            "n_stalls": self.n_stalls,
            "time_limit": self.time_limit,
            "time_limit_near": self.time_limit_near,
            "elapsed_s": round(self.elapsed_s, 3),
            "solver_time_s": self.solver_time_s,
            "last_line_wall_s": self.last_line_wall_s if self.last_line_wall_s is None
            else round(self.last_line_wall_s - self._t0, 3),
            "last_progress_wall_s": self.last_progress_wall_s if self.last_progress_wall_s is None
            else round(self.last_progress_wall_s - self._t0, 3),
            "finished": self.finished,
        }

    # ---- parsing -------------------------------------------------------

    def _event(self, kind: str, now: float, message: str, data: Dict[str, Any]) -> RunEvent:
        return RunEvent(kind=kind, wall_s=round(now - self._t0, 3),
                        message=message.strip()[:400], data=data)

    def _set_phase(self, phase: str, now: float, source: str = "log") -> List[RunEvent]:
        if phase == self.phase:
            return []
        prev = self.phase
        self.phase = phase
        return [self._event("phase", now, f"phase: {prev} -> {phase}",
                            {"phase": phase, "previous": prev, "source": source})]

    def _set_status(self, status: str, source: str, now: float, line: str,
                    extra: Optional[Dict[str, Any]] = None) -> List[RunEvent]:
        self.status = status
        self.status_source = source
        data = {"status": status, "source": source, "raw": line.strip()[:200]}
        if extra:
            data.update(extra)
        kind = "solver_status" if source == "solver" else "status"
        events: List[RunEvent] = []
        if status in TERMINAL_STATUSES:
            self.finished = True
            events.extend(self._set_phase("finished", now, source))
        events.append(self._event(kind, now, line, data))
        return events

    def _note_time(self, t: Optional[float]) -> None:
        if t is None:
            return
        if self.solver_time_s is None or t > self.solver_time_s:
            self.solver_time_s = t

    def _update_bounds(self, inc: Optional[float], bound: Optional[float]) -> bool:
        improved = False
        if inc is not None and not _close(inc, self.best_obj):
            self.best_obj = inc
            improved = True
        if bound is not None and not _close(bound, self.bound):
            self.bound = bound
            improved = True
        return improved

    def _improvement(self, improved: bool, now: float, t: Optional[float]) -> List[RunEvent]:
        if improved:
            self._last_improve_wall = now
            if t is not None:
                self._last_improve_solver_t = t
            self._stalled_improve = False
            return []
        if self.stall_seconds is None or self.finished or self._stalled_improve:
            return []
        if t is not None and self._last_improve_solver_t is not None:
            idle = t - self._last_improve_solver_t
        else:
            idle = now - self._last_improve_wall
        if idle >= self.stall_seconds:
            self._stalled_improve = True
            self.n_stalls += 1
            self.stall_reason = "no_improvement"
            return [self._event(
                "stall", now, f"no incumbent/bound improvement for {idle:.0f}s",
                {"reason": "no_improvement", "idle_s": round(idle, 1), "phase": self.phase,
                 "gap": self.gap, "incumbent": self.best_obj, "bound": self.bound},
            )]
        return []

    def _time_checks(self, now: float) -> List[RunEvent]:
        if self.time_limit is None or self.time_limit_near or self.finished:
            return []
        if self.solver_time_s is not None and self.last_progress_wall_s is not None:
            elapsed = self.solver_time_s + (now - self.last_progress_wall_s)
        elif self.solver_time_s is not None:
            elapsed = self.solver_time_s
        else:
            elapsed = now - self._t0
        if elapsed >= self.time_limit_fraction * self.time_limit:
            self.time_limit_near = True
            return [self._event(
                "time_limit_near", now,
                f"elapsed {elapsed:.0f}s of time_limit {self.time_limit:.0f}s "
                f"(>= {self.time_limit_fraction:.0%})",
                {"elapsed_s": round(elapsed, 1), "time_limit": self.time_limit,
                 "fraction": self.time_limit_fraction, "gap": self.gap,
                 "incumbent": self.best_obj, "bound": self.bound},
            )]
        return []

    def _lp_row(self, kind: str, it: int, obj: float, extra: Dict[str, Any],
                time_s: Optional[float], line: str, now: float) -> List[RunEvent]:
        improved = self.lp_objective is None or not _close(obj, self.lp_objective)
        self.lp_objective = obj
        self._note_time(time_s)
        self.last_progress_wall_s = now
        phase = "barrier" if kind == "barrier_iter" else "simplex"
        events = self._set_phase(phase, now)
        data = {"iter": it, "objective": obj, "time_s": time_s, "improved": improved}
        data.update(extra)
        events.append(self._event(kind, now, line, data))
        events.extend(self._improvement(improved, now, time_s))
        return events

    def _mip_row(self, inc: Optional[float], bound: Optional[float], gap: Optional[float],
                 nodes: Optional[int], open_nodes: Optional[int], time_s: Optional[float],
                 new_incumbent: bool, line: str, now: float,
                 extra: Optional[Dict[str, Any]] = None) -> List[RunEvent]:
        self.is_mip = True
        improved = self._update_bounds(inc, bound)
        if gap is not None:
            self.gap = gap
        if nodes is not None:
            self.nodes = nodes
        self._note_time(time_s)
        self.last_progress_wall_s = now
        if new_incumbent:
            self.n_incumbents += 1
        events = self._set_phase("branch_and_bound", now)
        data = {"incumbent": inc, "bound": bound, "gap": gap,
                "gap_pct": None if gap is None else round(gap * 100.0, 4),
                "nodes": nodes, "open_nodes": open_nodes, "time_s": time_s,
                "improved": improved, "new_incumbent": new_incumbent}
        if extra:
            data.update(extra)
        events.append(self._event("incumbent" if new_incumbent else "progress", now, line, data))
        events.extend(self._improvement(improved, now, time_s))
        return events

    def _parse(self, line: str, stripped: str, now: float) -> List[RunEvent]:  # noqa: C901
        if not stripped or stripped == "[stderr]":
            return []

        # ---- progress rows first: most frequent and most specific -------------
        m = _GRB_MIP_ROW.match(line)
        if m:
            return self._mip_row(
                _num(m["inc"]), _num(m["bound"]), _pct(m["gap"]), int(m["expl"]),
                int(m["unexpl"]), float(m["time"]), m["flag"] in ("H", "*"), line, now,
                {"flag": m["flag"]},
            )
        m = _HI_MIP_ROW.match(line)
        if m:
            return self._mip_row(
                _num(m["sol"]), _num(m["bound"]), _pct(m["gap"]), int(m["proc"]),
                int(m["inq"]), float(m["time"]), m["flag"] is not None, line, now,
                {"flag": m["flag"], "lp_iters": int(m["lpiters"]),
                 "explored_pct": float(m["expl"])},
            )
        m = _GRB_BARRIER_ROW.match(line)
        if m:
            return self._lp_row("barrier_iter", int(m[1]), float(m[2]),
                                {"dual": float(m[3])}, float(m[7]), line, now)
        m = _HI_SIMPLEX_ROW.match(line)
        if m:
            extra: Dict[str, Any] = {}
            for name, count, total in _HI_INFEAS.findall(m["infeas"] or ""):
                key = {"Pr": "primal_infeas", "Du": "dual_infeas", "Ph1": "phase1_infeas"}[name]
                extra[key] = int(count)
                extra[key + "_sum"] = float(total)
            return self._lp_row("simplex_iter", int(m["iter"]), float(m["obj"]),
                                extra, float(m["time"]), line, now)
        m = _GRB_SIMPLEX_ROW.match(line)
        if m:
            return self._lp_row("simplex_iter", int(m[1]), float(m[2]), {},
                                float(m[5]), line, now)

        # ---- Gurobi banners / phases / verdicts --------------------------------
        m = _GRB_BANNER.match(line)
        if m:
            self.solver, self.solver_version = "gurobi", m[1]
            return [self._event("solver_start", now, line,
                                {"solver": "gurobi", "version": m[1]})]
        m = _GRB_TIMELIMIT.match(line)
        if m:
            val = _num(m[1])
            if val is not None and self.time_limit is None:
                self.time_limit = val
            return []
        m = _GRB_MODEL.match(line)
        if m:
            self.model_size.update({"rows": int(m[1]), "cols": int(m[2]),
                                    "nonzeros": int(m[3]), "sense": m[4]})
            return [self._event("model_size", now, line, dict(self.model_size))]
        m = _GRB_VARTYPES.match(line)
        if m:
            n_int = int(m[2])
            self.model_size.update({"continuous": int(m[1]), "integer": n_int,
                                    "binary": int(m[3])})
            if n_int > 0:
                self.is_mip = True
            return []
        m = _GRB_PRESOLVED.match(line)
        if m:
            self.model_size["presolved"] = {"rows": int(m[1]), "cols": int(m[2]),
                                            "nonzeros": int(m[3])}
            return self._set_phase("presolve", now)
        if _GRB_PRESOLVE.match(line):
            return self._set_phase("presolve", now)
        m = _GRB_HEURISTIC.match(line)
        if m:
            inc = float(m[1])
            improved = self._update_bounds(inc, None)
            self.n_incumbents += 1
            self.is_mip = True
            events = [self._event("incumbent", now, line,
                                  {"incumbent": inc, "bound": self.bound, "gap": self.gap,
                                   "improved": improved, "new_incumbent": True,
                                   "source": "heuristic"})]
            events.extend(self._improvement(improved, now, None))
            return events
        if _GRB_BARRIER_HDR.match(line):
            return self._set_phase("barrier", now)
        if _GRB_CROSSOVER.match(line):
            return self._set_phase("crossover", now)
        if _GRB_SIMPLEX_HDR.match(line):
            return self._set_phase("simplex", now)
        m = _GRB_BARRIER_DONE.match(line)
        if m:
            self._note_time(float(m[2]))
            return []
        m = _GRB_SOLVED_IN.match(line)
        if m:
            self._note_time(float(m[2]))
            return []
        m = _GRB_ROOT_RELAX.match(line)
        if m:
            self.is_mip = True
            self.lp_objective = float(m[1])
            self._note_time(float(m[3]))
            events = self._set_phase("root_relaxation", now)
            events.append(self._event("root_relaxation", now, line,
                                      {"objective": float(m[1]), "iterations": int(m[2]),
                                       "time_s": float(m[3])}))
            return events
        if _GRB_MIP_HDR.match(line):
            self.is_mip = True
            return self._set_phase("branch_and_bound", now)
        m = _GRB_OPT_OBJ.match(line)
        if m:
            obj = float(m[1])
            if self.is_mip:
                # root LP relaxation of a MIP: informative, not the verdict
                self.lp_objective = obj
                return [self._event("root_relaxation", now, line,
                                    {"objective": obj, "final": False})]
            self.best_obj = obj
            events = [self._event("solution", now, line,
                                  {"objective": obj, "bound": None, "gap": None,
                                   "final": True})]
            events.extend(self._set_status("OPTIMAL", "solver", now, line))
            return events
        m = _GRB_EXPLORED.match(line)
        if m:
            self.nodes = int(m[1])
            self._note_time(float(m[3]))
            return [self._event("explored", now, line,
                                {"nodes": int(m[1]), "iterations": int(m[2]),
                                 "time_s": float(m[3])})]
        m = _GRB_FINAL_OPT.match(line)
        if m:
            return self._set_status("OPTIMAL", "solver", now, line,
                                    {"tolerance": _num(m[1])})
        m = _GRB_BEST.match(line)
        if m:
            obj, bound, gap = _num(m[1]), _num(m[2]), _pct(m[3])
            self._update_bounds(obj, bound)
            if gap is not None:
                self.gap = gap
            return [self._event("solution", now, line,
                                {"objective": obj, "bound": bound, "gap": gap,
                                 "gap_pct": None if gap is None else round(gap * 100, 4),
                                 "final": True})]
        for pattern, status in _GRB_STATUS:
            if pattern.match(line):
                return self._set_status(status, "solver", now, line)

        # ---- HiGHS banners / phases / verdicts ---------------------------------
        m = _HI_BANNER.match(line)
        if m:
            self.solver, self.solver_version = "highs", m[1]
            return [self._event("solver_start", now, line,
                                {"solver": "highs", "version": m[1]})]
        if _HI_MIP.match(line):
            self.is_mip = True
            self._hi_size = {}
            return []
        m = _HI_HAS.match(line)
        if m:
            if self.solver is None:
                self.solver = "highs"
            size = {"rows": int(m[2]), "cols": int(m[3]), "nonzeros": int(m[4]),
                    "kind": m[1]}
            if m[5] is not None:
                size.update({"integer": int(m[5]), "binary": int(m[6])})
            if m[1] != "LP":
                self.is_mip = True
            if "rows" not in self.model_size:
                self.model_size.update(size)
            return [self._event("model_size", now, line, size)]
        if _HI_SRC_LEGEND.match(line):
            return []  # the MIP log's source-letter legend, not a verdict
        if _HI_PRESOLVE_INF.match(line):
            return self._set_status("INFEASIBLE", "solver", now, line)
        if _HI_PRESOLVE_UNB.match(line):
            return self._set_status("UNBOUNDED", "solver", now, line)
        m = _HI_PRESOLVE_STATUS.match(line)
        if m:
            if self.solver is None:
                self.solver = "highs"
            status = normalize_status(m[1]) or "ERROR"
            if status in TERMINAL_STATUSES:
                return self._set_status(status, "solver", now, line,
                                        {"solver_status_text": m[1]})
            return self._set_phase("presolve", now)
        m = _HI_SIZE_PROGRESS.match(line)
        if m:
            events = self._set_phase("presolve", now)
            if "rows" not in self.model_size:
                self.model_size.update({"rows": int(m[1]), "cols": int(m[2]),
                                        "nonzeros": int(m[3])})
                events.append(self._event("model_size", now, line, dict(self.model_size)))
            else:
                self.model_size["presolved"] = {"rows": int(m[1]), "cols": int(m[2]),
                                                "nonzeros": int(m[3])}
            return events
        if self.solver == "highs" or self.is_mip:
            m = _HI_SIZE_ROWS.match(line)
            if m:
                self._hi_size["rows"] = int(m[1])
                return []
            m = _HI_SIZE_COLS.match(line)
            if m:
                self._hi_size["cols"] = int(m[1])
                if m[2] is not None:
                    self._hi_size.update({"binary": int(m[2]), "integer": int(m[3]),
                                          "implied_int": int(m[4]), "continuous": int(m[5])})
                    if int(m[2]) + int(m[3]) > 0:
                        self.is_mip = True
                return []
            m = _HI_SIZE_NZ.match(line)
            if m and self._hi_size:
                self._hi_size["nonzeros"] = int(m[1])
                size = dict(self._hi_size)
                self._hi_size = {}
                if "rows" not in self.model_size:
                    self.model_size.update(size)
                else:
                    self.model_size["presolved"] = size
                return [self._event("model_size", now, line, size)]
        if _HI_PRESOLVE.match(line):
            return self._set_phase("presolve", now)
        if _HI_LP.match(line):
            return self._set_phase("simplex", now)
        if _HI_IPM.match(line):
            return self._set_phase("barrier", now)
        if _HI_MIP_HDR.match(line):
            self.is_mip = True
            return self._set_phase("branch_and_bound", now)
        m = _HI_MODEL_STATUS.match(line)
        if m:
            status = normalize_status(m[1]) or "ERROR"
            return self._set_status(status, "solver", now, line, {"solver_status_text": m[1]})
        m = _HI_OBJ.match(line)
        if m:
            if self.status in ("INFEASIBLE", "UNBOUNDED", "ERROR"):
                return []  # HiGHS prints a meaningless 0 objective after these
            obj = float(m[1])
            self._update_bounds(obj, None)
            return [self._event("solution", now, line,
                                {"objective": obj, "bound": self.bound, "gap": self.gap,
                                 "final": True})]
        m = _HI_RUNTIME.match(line)
        if m:
            self._note_time(float(m[1]))
            return []
        if _HI_REPORT.match(line):
            self._in_report = True
            return []
        if self._in_report:
            if line[:1] not in (" ", "\t"):
                self._in_report = False
            else:
                m = _HI_REPORT_STATUS.match(line)
                if m:
                    status = normalize_status(m[1]) or "ERROR"
                    return self._set_status(status, "solver", now, line,
                                            {"solver_status_text": m[1]})
                m = _HI_REPORT_PRIMAL.match(line)
                if m:
                    self._update_bounds(_num(m[1]), None)
                    return []
                m = _HI_REPORT_DUAL.match(line)
                if m:
                    self._update_bounds(None, _num(m[1]))
                    return []
                m = _HI_REPORT_GAP.match(line)
                if m:
                    gap = _pct(m[1])
                    if gap is not None:
                        self.gap = gap
                    if self.best_obj is None and self.bound is None:
                        return []  # infeasible / no solution: nothing to report
                    return [self._event("solution", now, line,
                                        {"objective": self.best_obj, "bound": self.bound,
                                         "gap": gap,
                                         "gap_pct": None if gap is None else round(gap * 100, 4),
                                         "final": True})]
                m = _HI_REPORT_TIMING.match(line)
                if m:
                    self._note_time(float(m[1]))
                    return []
                m = _HI_REPORT_NODES.match(line)
                if m:
                    self.nodes = int(m[1])
                    return []
                return []

        # ---- tracebacks, errors, warnings --------------------------------------
        if _PY_TB_START.match(line):
            self._py_tb = {"frames": [], "message": None}
            return []
        m = _JL_ERROR.match(line)
        if m:
            self._jl_tb = {"message": stripped, "frames": []}
            self._jl_collecting = False
            self.n_errors += 1
            self.errors.append(stripped[:300])
            return [self._event("error", now, line,
                                {"text": stripped[:300], "language": "julia"})]
        if _WARNING.match(line):
            self.n_warnings += 1
            self.warnings.append(stripped[:300])
            return [self._event("warning", now, line, {"text": stripped[:300]})]
        if _ERROR.match(line):
            self.n_errors += 1
            self.errors.append(stripped[:300])
            return [self._event("error", now, line, {"text": stripped[:300]})]
        if _LICENSE.search(line) and _LICENSE_BAD.search(line):
            self.n_errors += 1
            self.errors.append(stripped[:300])
            return [self._event("error", now, line,
                                {"text": stripped[:300], "reason": "license"})]

        # ---- generic wrapper markers -------------------------------------------
        origin_here: Optional[str] = None
        m = _ORIGIN_TOKEN.search(line)
        if m:
            origin_here = m[1].lower()
            self.error_origin = origin_here  # may share the line with a status token
        m = _STAGE_TOKEN.search(line)
        if m and not _GENERIC_STATUS.search(line):
            return self._set_phase(m[1].strip().rstrip(".,;"), now, source="marker")
        if _PREFLIGHT.search(line):
            bad = bool(_PREFLIGHT_BAD.search(line))
            ok = bool(_PREFLIGHT_OK.search(line))
            passed: Optional[bool] = False if bad else (True if ok else None)
            events = self._set_phase("preflight", now, source="marker") if not self.finished else []
            events.append(self._event("preflight", now, line, {"passed": passed}))
            if bad:
                self.error_origin = self.error_origin or "preflight"
                events.extend(self._set_status("ERROR", "marker", now, line,
                                               {"error_origin": self.error_origin}))
            return events
        if _GENERIC_STATUS.search(line):
            status = status_from_marker(line)
            if status is not None:
                extra = {"error_origin": origin_here} if origin_here else None
                return self._set_status(status, "marker", now, line, extra)
        return []

    # ---- tracebacks ----------------------------------------------------

    def _finalise_tb(self, language: str, tb: Dict[str, Any], now: float,
                     line: str) -> RunEvent:
        frames = tb.get("frames", [])[-_MAX_FRAMES:]
        self.traceback = {"language": language, "message": tb.get("message"),
                          "frames": frames}
        if tb.get("at"):
            self.traceback["at"] = tb["at"]
        if language == "python":
            self.n_errors += 1
            self.errors.append(str(tb.get("message"))[:300])
        self.phase = "error"
        return self._event("traceback", now, line,
                           {"language": language, "message": tb.get("message"),
                            "frames": frames, "n_frames": len(tb.get("frames", []))})

    def _feed_py_tb(self, line: str, stripped: str, now: float) -> Optional[RunEvent]:
        tb = self._py_tb
        assert tb is not None
        if not stripped or _PY_TB_START.match(stripped) or _PY_CHAIN.match(stripped):
            return None
        if line[:1] in (" ", "\t"):
            m = _PY_FRAME.match(line)
            if m:
                tb["frames"].append({"file": m[1], "line": int(m[2]), "func": m[3]})
                if len(tb["frames"]) > 4 * _MAX_FRAMES:
                    del tb["frames"][: -2 * _MAX_FRAMES]
            return None
        tb["message"] = stripped
        self._py_tb = None
        return self._finalise_tb("python", tb, now, line)

    def _feed_jl_tb(self, line: str, stripped: str, now: float):
        tb = self._jl_tb
        assert tb is not None
        if not self._jl_collecting:
            if stripped == "Stacktrace:":
                self._jl_collecting = True
                return True, None
            m = _JL_EXPR.match(stripped)
            if m:
                tb["at"] = m[1]
                self._jl_tb = None
                return True, self._finalise_tb("julia", tb, now, line)
            # a bare ERROR line with no stacktrace: nothing more to collect
            self._jl_tb = None
            return False, None
        m = _JL_FRAME.match(line)
        if m:
            tb["frames"].append({"n": int(m[1]), "func": m[2][:200]})
            if len(tb["frames"]) > 4 * _MAX_FRAMES:
                del tb["frames"][: -2 * _MAX_FRAMES]
            return True, None
        m = _JL_LOC.match(line)
        if m:
            if tb["frames"]:
                tb["frames"][-1]["at"] = m[1][:200]
            return True, None
        if not stripped:
            return True, None
        m = _JL_EXPR.match(stripped)
        if m:
            tb["at"] = m[1]
            self._jl_tb = None
            return True, self._finalise_tb("julia", tb, now, line)
        # anything else ends the trace; let the caller parse this line normally
        self._jl_tb = None
        return False, self._finalise_tb("julia", tb, now, tb["message"])


# --------------------------------------------------------------------------
# RunMonitor
# --------------------------------------------------------------------------

OnEvent = Callable[[RunEvent], None]


class RunMonitor:
    """Adapter-facing wrapper: watcher + rolling summary + fan-out + persistence.

    Typical adapter wiring::

        monitor = RunMonitor(run_dir, on_event=on_event, abort=abort,
                             time_limit=config.get("time_limit"))
        result = stream_command(cmd, cwd=..., log_path=run_dir / "solver.log",
                                on_line=monitor.on_line, on_tick=monitor.tick,
                                abort=monitor.abort_event, pid_file=run_dir / "run.pid")
        summary = monitor.finish(result)          # -> Execution(monitor=summary)

    ``monitor.json`` is rewritten (atomically) at most every ``persist_interval``
    seconds and immediately on notable events, so ``python -m framework.watch``
    can read a consistent snapshot while the solve is still running.
    """

    def __init__(
        self,
        run_dir: Optional[str | Path] = None,
        on_event: Optional[OnEvent] = None,
        *,
        watcher: Optional[LogWatcher] = None,
        abort: Optional[threading.Event] = None,
        persist_interval: float = 5.0,
        keep_events: int = 200,
        clock: Optional[Callable[[], float]] = None,
        source: str = "live",
        **watcher_kwargs: Any,
    ) -> None:
        if watcher is not None and watcher_kwargs:
            raise ValueError("pass either a watcher or watcher kwargs, not both")
        self.watcher = watcher or LogWatcher(clock=clock, **watcher_kwargs)
        self.run_dir = Path(run_dir) if run_dir is not None else None
        self.on_event = on_event
        self.abort_event = abort if abort is not None else threading.Event()
        self.abort_reason: Optional[str] = None
        self.source = source
        self.events: Deque[RunEvent] = deque(maxlen=keep_events)
        self.event_counts: Counter = Counter()
        self.n_events = 0
        self.callback_errors = 0
        self.returncode: Optional[int] = None
        self.timed_out = False
        self.wall_seconds: Optional[float] = None
        self._clock = clock or time.monotonic
        self._persist_interval = persist_interval
        self._last_persist: Optional[float] = None
        self._dirty = False
        self._lock = threading.RLock()

    # ---- line / tick intake ----------------------------------------------

    def on_line(self, line: str) -> List[RunEvent]:
        """``stream_command``'s ``on_line`` callback."""
        with self._lock:
            events = self.watcher.feed(line)
            self._dispatch(events)
            self._maybe_persist(events)
            return events

    feed = on_line

    def feed_many(self, lines: Iterable[str]) -> List[RunEvent]:
        out: List[RunEvent] = []
        for line in lines:
            out.extend(self.on_line(line))
        return out

    def tick(self) -> List[RunEvent]:
        """``stream_command``'s ``on_tick`` callback (stall / time-limit checks)."""
        with self._lock:
            events = self.watcher.tick()
            self._dispatch(events)
            self._maybe_persist(events)
            return events

    def request_abort(self, reason: str = "requested") -> None:
        """Ask the streaming adapter to terminate the solver (via ``abort_event``)."""
        self.abort_reason = reason
        self.abort_event.set()

    def finish(self, stream_result: Any = None) -> Dict[str, Any]:
        """End of stream: flush, fold in the process result, persist, summarise."""
        with self._lock:
            events = self.watcher.finish()
            self._dispatch(events)
            if stream_result is not None:
                self.returncode = getattr(stream_result, "returncode", None)
                self.timed_out = bool(getattr(stream_result, "timed_out", False))
                self.wall_seconds = getattr(stream_result, "wall_seconds", None)
                if getattr(stream_result, "aborted", False):
                    self.abort_event.set()
                    if self.abort_reason is None:
                        self.abort_reason = getattr(stream_result, "abort_reason", None) or "aborted"
            summary = self.summary()
            if self.run_dir is not None:
                self.save()
            return summary

    # ---- summary / persistence ---------------------------------------------

    def summary(self) -> Dict[str, Any]:
        s = self.watcher.snapshot()
        s.update({
            "schema_version": SCHEMA_VERSION,
            "source": self.source,
            "n_events": self.n_events,
            "event_counts": dict(self.event_counts),
            "callback_errors": self.callback_errors,
            "aborted": self.abort_event.is_set(),
            "abort_reason": self.abort_reason,
            "returncode": self.returncode,
            "timed_out": self.timed_out,
            "wall_seconds": self.wall_seconds,
            "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        })
        return s

    @property
    def path(self) -> Optional[Path]:
        return None if self.run_dir is None else self.run_dir / "monitor.json"

    def save(self, path: Optional[str | Path] = None) -> Path:
        target = Path(path) if path is not None else self.path
        if target is None:
            raise ValueError("RunMonitor.save needs a path or a run_dir")
        target.parent.mkdir(parents=True, exist_ok=True)
        payload = self.summary()
        payload["recent_events"] = [e.to_dict() for e in self.events]
        tmp = target.with_name(target.name + ".tmp")
        tmp.write_text(json.dumps(payload, indent=2, default=str))
        os.replace(tmp, target)
        self._last_persist = self._clock()
        self._dirty = False
        return target

    @staticmethod
    def load(path: str | Path) -> Dict[str, Any]:
        return json.loads(Path(path).read_text())

    # ---- internals ----------------------------------------------------------

    def _dispatch(self, events: List[RunEvent]) -> None:
        for ev in events:
            self.events.append(ev)
            self.event_counts[ev.kind] += 1
            self.n_events += 1
            self._dirty = True
            if self.on_event is not None:
                try:
                    self.on_event(ev)
                except Exception:  # noqa: BLE001 - a listener bug must not break the run
                    self.callback_errors += 1

    def _maybe_persist(self, events: List[RunEvent]) -> None:
        if self.run_dir is None or not self._dirty:
            return
        now = self._clock()
        notable = any(ev.kind in NOTABLE_KINDS for ev in events)
        due = self._last_persist is None or (now - self._last_persist) >= self._persist_interval
        if notable or due:
            try:
                self.save()
            except OSError:
                pass  # monitoring must never break the run itself


# --------------------------------------------------------------------------
# replay helpers
# --------------------------------------------------------------------------

def replay_log(
    source: str | Path | Iterable[str],
    on_event: Optional[OnEvent] = None,
    run_dir: Optional[str | Path] = None,
    **watcher_kwargs: Any,
) -> RunMonitor:
    """Feed a saved log (path, or an iterable of lines) through a fresh monitor.

    Returns the finished :class:`RunMonitor`; ``.summary()`` is the rolling
    summary, ``.events`` the last ``keep_events`` events, and every event was
    also delivered to ``on_event``. Wall-clock stall detection is meaningless
    in a replay, so only the solver-time based check can fire. With
    ``run_dir`` the finished summary is written once to ``<run_dir>/monitor.json``.
    """
    if isinstance(source, (str, Path)):
        text = Path(source).read_text(encoding="utf-8", errors="replace")
        lines: Iterable[str] = text.splitlines()
    else:
        lines = source
    monitor = RunMonitor(run_dir=None, on_event=on_event, source="replay",
                         persist_interval=float("inf"), **watcher_kwargs)
    monitor.feed_many(lines)
    monitor.finish()
    if run_dir is not None:
        monitor.run_dir = Path(run_dir)
        try:
            monitor.save()
        except OSError:
            pass
    return monitor
