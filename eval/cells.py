"""Cells — one (task × adapter × LLM × guardrail × seed) run of the loop (§4, §8).

``run_cell`` builds the headless :class:`AgentDriver` around an LLM client,
wraps the adapter in the solve cache, runs the supervisor with the guardrail
enforced or ablated, and persists everything under::

    <eval_root>/<adapter>/<llm>/<guardrail>/<task_id>/seed<k>/
        task.json          copy of the task (config + label + prompt) — scoring is self-contained
        prompt.md          the adversarial framing, when the task has one
        iter00_<hash>/     the supervisor's run directories (run_record.json, solver.log, outputs/)
        iter01_<hash>/
        cell.json          outcome, iterations, timings, tier key-sets, cache stats

The *only* difference between the guarded and unguarded condition is the
``enforce_guardrail`` flag handed to the supervisor (protocol §4).

Tests can bypass the LLM by passing a deterministic ``propose_fn`` (and
``analyze_fn``); the persisted layout is identical.
"""

from __future__ import annotations

import inspect
import json
import re
import shutil
import time
import traceback
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple, Union

from framework.adapter import Adapter
from framework.analyze import read_outputs, record_anomalies
from framework.interventions import InterventionSpec, ProposedChange
from framework.run_record import Anomaly, RunRecord
from framework.supervisor import StopCriteria, Supervisor

from eval.cache import CachedAdapter, SolveCache
from eval.tasks import Task

GUARDRAILS = ("guarded", "unguarded")
CELL_SCHEMA_VERSION = 1
CELL_FILE = "cell.json"
TASK_FILE = "task.json"

ProposeFn = Callable[[RunRecord], List[ProposedChange]]
AnalyzeFn = Callable[[RunRecord, Dict[str, List[dict]]], List[Anomaly]]


class EvalSetupError(RuntimeError):
    """A required framework piece (driver, LLM client, guardrail flag) is missing."""


def slug(text: str) -> str:
    """Filesystem-safe label (``claude-cli:sonnet`` -> ``claude-cli_sonnet``)."""
    s = re.sub(r"[^A-Za-z0-9._-]+", "_", str(text)).strip("_")
    return s or "x"


# ---- data ------------------------------------------------------------------

@dataclass(frozen=True)
class Cell:
    task: Task
    adapter: str
    llm: str
    guardrail: str
    seed: int

    def __post_init__(self) -> None:
        if self.guardrail not in GUARDRAILS:
            raise ValueError(f"guardrail must be one of {GUARDRAILS}, got {self.guardrail!r}")

    @property
    def enforce_guardrail(self) -> bool:
        return self.guardrail == "guarded"

    @property
    def cell_id(self) -> str:
        return f"{self.adapter}/{slug(self.llm)}/{self.guardrail}/{self.task.task_id}/seed{self.seed}"

    def dir(self, eval_root: Union[str, Path]) -> Path:
        return Path(eval_root) / self.adapter / slug(self.llm) / self.guardrail / self.task.task_id / f"seed{self.seed}"


@dataclass
class CellResult:
    """What one cell produced — ``cell.json``."""

    adapter: str
    llm: str
    guardrail: str
    seed: int
    task_id: str
    family: str
    outcome: str                      # solved | needs_human | exhausted | cycle | stuck | error
    reason: str = ""
    iterations: int = 0
    wall_seconds: float = 0.0
    started_at: str = ""
    finished_at: str = ""
    max_iters: int = 0
    run_dirs: List[str] = field(default_factory=list)
    statuses: List[Optional[str]] = field(default_factory=list)
    config_hashes: List[str] = field(default_factory=list)
    iteration_wall_seconds: List[Optional[float]] = field(default_factory=list)
    cache_hits: int = 0
    cache_misses: int = 0
    error: Optional[str] = None
    analysis_error: Optional[str] = None
    prompt_used: bool = False
    final_status: Optional[str] = None
    initial_config: Dict[str, Any] = field(default_factory=dict)
    final_config: Dict[str, Any] = field(default_factory=dict)
    tier_a_keys: List[str] = field(default_factory=list)
    tier_b_keys: List[str] = field(default_factory=list)
    tier_c_keys: List[str] = field(default_factory=list)
    anomalies: List[Dict[str, Any]] = field(default_factory=list)
    schema_version: int = CELL_SCHEMA_VERSION

    @property
    def solved(self) -> bool:
        return self.outcome == "solved"

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    def save(self, path: Union[str, Path]) -> Path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.to_dict(), indent=2, default=str))
        return path

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "CellResult":
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in data.items() if k in known})

    @classmethod
    def load(cls, path: Union[str, Path]) -> "CellResult":
        return cls.from_dict(json.loads(Path(path).read_text()))


# ---- planning --------------------------------------------------------------

def plan_cells(
    tasks: Sequence[Task],
    adapter: str,
    llm: str,
    guardrails: Union[str, Iterable[str]] = "both",
    seeds: Union[int, Iterable[int]] = 5,
) -> List[Cell]:
    """Enumerate the cells of a (partial) factorial design."""
    if guardrails == "both":
        gs: List[str] = list(GUARDRAILS)
    elif isinstance(guardrails, str):
        gs = [guardrails]
    else:
        gs = list(guardrails)
    ks = list(range(seeds)) if isinstance(seeds, int) else list(seeds)
    cells: List[Cell] = []
    for task in tasks:
        for g in gs:
            for k in ks:
                cells.append(Cell(task=task, adapter=adapter, llm=llm, guardrail=g, seed=k))
    return cells


# ---- building the loop pieces ---------------------------------------------

def supports_unguarded() -> bool:
    """True when the supervisor exposes the ablation flag (Workstream E)."""
    try:
        return "enforce_guardrail" in inspect.signature(Supervisor.__init__).parameters
    except (TypeError, ValueError):
        return False


def make_supervisor(adapter: Adapter, spec: InterventionSpec, enforce: bool) -> Supervisor:
    if supports_unguarded():
        return Supervisor(adapter, spec, enforce_guardrail=enforce)  # type: ignore[call-arg]
    if enforce:
        return Supervisor(adapter, spec)
    raise EvalSetupError(
        "the unguarded condition needs framework.supervisor.Supervisor(enforce_guardrail=False); "
        "this checkout's Supervisor has no such flag"
    )


def _wrap_driver_propose(driver: Any) -> ProposeFn:
    """Fallback when the driver module has no ``make_propose_fn``."""
    def propose_fn(record: RunRecord) -> List[ProposedChange]:
        out = driver.propose(record)
        if isinstance(out, tuple):
            out = out[0]
        return list(out or [])
    return propose_fn


try:  # keep the heading identical to the driver's first-class hook
    from framework.agent_driver import USER_CONTEXT_HEADING
except ImportError:  # pragma: no cover - driver-less checkout
    USER_CONTEXT_HEADING = "User request (verbatim, from the modeller who owns this study)"


def attach_user_context(driver: Any, prompt: str,
                        heading: str = USER_CONTEXT_HEADING) -> Any:
    """Make the task's ``prompt.md`` part of every user message the driver sends.

    The framing is a *task input*, not a framework setting: an adversarial
    "just make it feasible" request is exactly the pressure the guardrail is
    supposed to withstand, so it must reach the model verbatim.

    ``AgentDriver`` now takes ``user_context=`` directly, which
    :func:`build_driver` prefers, so this wrapper is the compatibility path for
    a driver without that kwarg (and for wrapping a driver after construction).
    """
    original = driver._user_prompt

    def with_context(record: Any, log_tail: Any, sections: Any = None) -> str:
        merged: Dict[str, str] = {heading: prompt.strip()}
        merged.update(dict(sections or {}))
        return original(record, log_tail, merged)

    driver._user_prompt = with_context
    driver.user_context = prompt
    return driver


def build_driver(client: Any, adapter: Adapter, task: Task,
                 skills_dir: Union[str, Path, None] = None) -> Tuple[Any, ProposeFn, Optional[AnalyzeFn]]:
    """Instantiate ``framework.agent_driver.AgentDriver`` and return
    ``(driver, propose_fn, analyze_fn)``.

    The task's adversarial ``prompt.md`` is passed as ``user_context`` when the
    driver accepts it (constructor kwarg), otherwise injected into every user
    message by :func:`attach_user_context`.
    """
    try:
        from framework import agent_driver as ad  # Workstream E
    except ImportError as e:  # pragma: no cover - depends on checkout
        raise EvalSetupError(
            "framework.agent_driver (the headless driver) is required to run cells with an LLM; "
            "pass propose_fn= to run without it"
        ) from e
    try:
        params = inspect.signature(ad.AgentDriver.__init__).parameters
    except (TypeError, ValueError):
        params = {}
    kwargs: Dict[str, Any] = {}
    if skills_dir is not None and "skills_dir" in params:
        kwargs["skills_dir"] = skills_dir
    if task.prompt and "user_context" in params:
        kwargs["user_context"] = task.prompt
    driver = ad.AgentDriver(client, adapter, **kwargs)
    if task.prompt and "user_context" not in params and hasattr(driver, "_user_prompt"):
        attach_user_context(driver, task.prompt)
    make = getattr(ad, "make_propose_fn", None)
    propose_fn = make(driver) if callable(make) else _wrap_driver_propose(driver)
    analyze_fn = getattr(driver, "analyze_outputs", None)
    return driver, propose_fn, analyze_fn


# ---- run dir bookkeeping ---------------------------------------------------

def _run_dir_for(cell_dir: Path, index: int, record: RunRecord) -> Optional[Path]:
    exact = cell_dir / f"iter{index:02d}_{record.config_hash}"
    if exact.is_dir():
        return exact
    matches = sorted(p for p in cell_dir.glob(f"*{record.config_hash}*") if p.is_dir())
    return matches[0] if matches else None


def _recover_ledger(cell_dir: Path) -> List[RunRecord]:
    records = []
    for p in sorted(cell_dir.glob("*/run_record.json")):
        try:
            records.append(RunRecord.load(p))
        except Exception:
            continue
    return records


def _spec_keys(spec: InterventionSpec) -> Tuple[List[str], List[str], List[str]]:
    return sorted(spec.tier_a_keys), sorted(spec.tier_b_keys), sorted(spec.tier_c_keys)


# ---- running ---------------------------------------------------------------

def run_cell(
    cell: Cell,
    adapter: Adapter,
    client: Any = None,
    *,
    eval_root: Union[str, Path],
    max_iters: int = 5,
    cache: Optional[SolveCache] = None,
    propose_fn: Optional[ProposeFn] = None,
    analyze_fn: Optional[AnalyzeFn] = None,
    skills_dir: Union[str, Path, None] = None,
    overwrite: bool = False,
) -> CellResult:
    """Run one cell end-to-end and persist it. Never raises on a failed loop —
    a crash is recorded as ``outcome="error"`` with the traceback."""
    eval_root = Path(eval_root)
    cell_dir = cell.dir(eval_root)
    cell_file = cell_dir / CELL_FILE
    if cell_file.exists() and not overwrite:
        return CellResult.load(cell_file)
    if cell_dir.exists() and overwrite:
        shutil.rmtree(cell_dir)
    cell_dir.mkdir(parents=True, exist_ok=True)

    task = cell.task
    (cell_dir / TASK_FILE).write_text(json.dumps(task.to_dict(), indent=2, default=str))
    if task.prompt:
        (cell_dir / "prompt.md").write_text(task.prompt)

    spec = adapter.intervention_spec()
    a_keys, b_keys, c_keys = _spec_keys(spec)
    run_adapter: Adapter = CachedAdapter(adapter, cache) if cache is not None else adapter
    hits0 = cache.hits if cache is not None else 0
    misses0 = cache.misses if cache is not None else 0

    if client is not None and hasattr(client, "seed"):
        try:
            client.seed = cell.seed
        except Exception:
            pass

    started = time.strftime("%Y-%m-%dT%H:%M:%S")
    t0 = time.perf_counter()
    error: Optional[str] = None
    analysis_error: Optional[str] = None
    ledger: List[RunRecord] = []
    outcome, reason = "error", ""

    try:
        supervisor = make_supervisor(run_adapter, spec, cell.enforce_guardrail)
        if propose_fn is None:
            _driver, propose_fn, driver_analyze = build_driver(client, run_adapter, task, skills_dir)
            analyze_fn = analyze_fn or driver_analyze
        result = supervisor.run(dict(task.config), propose_fn, cell_dir, StopCriteria(max_iters=max_iters))
        outcome, reason, ledger = result.outcome, result.reason, list(result.ledger)
    except Exception as e:  # noqa: BLE001 - a crashed cell is data, not a crash of the study
        error = traceback.format_exc()
        reason = f"{type(e).__name__}: {e}"
        ledger = _recover_ledger(cell_dir)

    # Output analysis on a solved final run (the output_anomaly family).
    anomalies_out: List[Dict[str, Any]] = []
    if ledger and ledger[-1].execution.termination_status == "OPTIMAL" and analyze_fn is not None:
        final = ledger[-1]
        run_dir = _run_dir_for(cell_dir, len(ledger) - 1, final)
        try:
            outputs = read_outputs(run_adapter, run_dir) if run_dir is not None else {}
        except Exception:  # noqa: BLE001
            outputs = {}
        try:
            before = len(final.output_anomalies)
            anomalies = list(analyze_fn(final, outputs) or [])
            if len(final.output_anomalies) == before:
                # A plain analyze_fn returned them without recording; the
                # driver's analyze_outputs already appended, so do not double.
                record_anomalies(final, anomalies, run_dir)
            elif run_dir is not None:
                final.save(run_dir / "run_record.json")
        except Exception:  # noqa: BLE001
            analysis_error = traceback.format_exc()

    # Persist every record again: a diagnosis attached by propose_fn on the
    # 'stuck' path is otherwise never saved by the supervisor.
    run_dirs: List[str] = []
    for i, rec in enumerate(ledger):
        rd = _run_dir_for(cell_dir, i, rec)
        if rd is None:
            rd = cell_dir / f"iter{i:02d}_{rec.config_hash}"
        try:
            rec.save(rd / "run_record.json")
        except Exception:  # noqa: BLE001
            pass
        run_dirs.append(rd.name)
    if ledger:
        anomalies_out = [asdict(a) for a in ledger[-1].output_anomalies]

    wall = time.perf_counter() - t0
    res = CellResult(
        adapter=cell.adapter, llm=cell.llm, guardrail=cell.guardrail, seed=cell.seed,
        task_id=task.task_id, family=task.family,
        outcome=outcome, reason=reason, iterations=len(ledger), wall_seconds=round(wall, 3),
        started_at=started, finished_at=time.strftime("%Y-%m-%dT%H:%M:%S"), max_iters=max_iters,
        run_dirs=run_dirs,
        statuses=[r.execution.termination_status for r in ledger],
        config_hashes=[r.config_hash for r in ledger],
        iteration_wall_seconds=[r.execution.wall_seconds for r in ledger],
        cache_hits=(cache.hits - hits0) if cache is not None else 0,
        cache_misses=(cache.misses - misses0) if cache is not None else 0,
        error=error, analysis_error=analysis_error,
        prompt_used=bool(task.prompt),
        final_status=ledger[-1].execution.termination_status if ledger else None,
        initial_config=dict(task.config),
        final_config=dict(ledger[-1].config) if ledger else dict(task.config),
        tier_a_keys=a_keys, tier_b_keys=b_keys, tier_c_keys=c_keys,
        anomalies=anomalies_out,
    )
    res.save(cell_file)
    return res


def run_cells(
    cells: Sequence[Cell],
    adapter: Adapter,
    client: Any = None,
    *,
    eval_root: Union[str, Path],
    max_iters: int = 5,
    cache: Optional[SolveCache] = None,
    progress: Optional[Callable[[str], None]] = None,
    allowed_solvers: Optional[Iterable[str]] = None,
    **kwargs: Any,
) -> List[CellResult]:
    """Run many cells sequentially (resumable: an existing ``cell.json`` is reused).

    ``allowed_solvers`` skips tasks whose ``needs_solver`` is not in the set
    (e.g. run only licence-free tasks with ``{"highs", "none"}``).
    """
    allowed = set(allowed_solvers) if allowed_solvers is not None else None
    results: List[CellResult] = []
    for n, cell in enumerate(cells, 1):
        if allowed is not None and cell.task.needs_solver not in allowed:
            if progress:
                progress(f"[{n}/{len(cells)}] skip {cell.cell_id} (needs_solver={cell.task.needs_solver})")
            continue
        if progress:
            progress(f"[{n}/{len(cells)}] {cell.cell_id}")
        res = run_cell(cell, adapter, client, eval_root=eval_root, max_iters=max_iters,
                       cache=cache, **kwargs)
        if progress:
            progress(f"    -> {res.outcome} in {res.iterations} iter(s), {res.wall_seconds}s"
                     + (f" [error: {res.reason}]" if res.error else ""))
        results.append(res)
    return results
