"""Benchmark tasks — the labelled inputs of the study (protocol §5).

A task is a directory ``examples/<adapter>/benchmark/<task_id>/`` holding

- ``config.json``   — the (deliberately broken) model config the agent starts from,
- ``expected.json`` — the ground-truth label in the §5 schema, plus ``needs_solver``
                      and ``notes`` (and a few optional extensions, see
                      :class:`TaskLabel`),
- ``prompt.md``     — optional adversarial user framing ("please just make it
                      feasible") handed to the agent driver as user context.

Nothing here knows any model: the adapter name is just a directory segment.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Union

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_BENCHMARK_PARENT = REPO_ROOT / "examples"

FAMILIES = (
    "tierA_fixable",
    "tierB_fixable",
    "tierC_infeasible",
    "preflight_error",
    "output_anomaly",
)
TERMINAL_OUTCOMES = ("solved", "needs_human", "flagged")
STATUSES = ("OPTIMAL", "INFEASIBLE", "TIME_LIMIT", "ERROR")
TIERS = ("A", "B", "C")
ERROR_ORIGINS = ("preflight", "solver", "runtime")
SOLVERS = ("highs", "gurobi", "none")


@dataclass
class TaskLabel:
    """The per-task ground truth (protocol §5 schema + benchmark extensions).

    The first eight fields are exactly the §5 schema. ``needs_solver`` and
    ``notes`` are required by the build brief. The remaining fields are optional
    extensions used by the scorer:

    - ``expected_fix_keys``: config keys the *correct* fix touches (lets the
      scorer report whether the agent proposed the right lever, independent of
      whether the framework let it apply).
    - ``alternative_fix_tiers``: tiers that would *also* legitimately resolve the
      task (e.g. a time-limited MILP can be fixed by Tier B ``relax_uc`` or by a
      Tier A ``time_limit``); Tier Correctness is reported strict (``expected_tier``
      only) and lenient (either).
    - ``tolerated_anomaly_metrics``: on clean baselines, flags on these metrics
      are not counted as false positives (a documented, real feature of the data).
    """

    task_id: str
    family: str
    expected_status: Optional[str] = None
    expected_error_origin: Optional[str] = None
    expected_root_cause_category: Optional[str] = None
    expected_tier: Optional[str] = None
    expected_terminal_outcome: Optional[str] = None
    planted_anomaly_metric: Optional[str] = None
    needs_solver: str = "highs"
    notes: str = ""
    expected_fix_keys: List[str] = field(default_factory=list)
    alternative_fix_tiers: List[str] = field(default_factory=list)
    tolerated_anomaly_metrics: List[str] = field(default_factory=list)
    extra: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "TaskLabel":
        known = {f.name for f in fields(cls)} - {"extra"}
        kwargs = {k: v for k, v in data.items() if k in known}
        extra = {k: v for k, v in data.items() if k not in known}
        # Tolerate lists given as null.
        for k in ("expected_fix_keys", "alternative_fix_tiers", "tolerated_anomaly_metrics"):
            if kwargs.get(k) is None:
                kwargs[k] = []
        return cls(extra=extra, **kwargs)

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        extra = d.pop("extra") or {}
        d.update(extra)
        return d

    def validate(self) -> List[str]:
        """Return schema problems (empty list = valid)."""
        errs: List[str] = []
        if not self.task_id:
            errs.append("task_id missing")
        if self.family not in FAMILIES:
            errs.append(f"family {self.family!r} not in {FAMILIES}")
        if self.expected_status is not None and self.expected_status not in STATUSES:
            errs.append(f"expected_status {self.expected_status!r} not in {STATUSES}")
        if self.expected_error_origin is not None and self.expected_error_origin not in ERROR_ORIGINS:
            errs.append(f"expected_error_origin {self.expected_error_origin!r} not in {ERROR_ORIGINS}")
        if self.expected_tier is not None and self.expected_tier not in TIERS:
            errs.append(f"expected_tier {self.expected_tier!r} not in {TIERS}")
        if self.expected_terminal_outcome is not None and self.expected_terminal_outcome not in TERMINAL_OUTCOMES:
            errs.append(
                f"expected_terminal_outcome {self.expected_terminal_outcome!r} not in {TERMINAL_OUTCOMES}"
            )
        if self.needs_solver not in SOLVERS:
            errs.append(f"needs_solver {self.needs_solver!r} not in {SOLVERS}")
        for t in self.alternative_fix_tiers:
            if t not in TIERS:
                errs.append(f"alternative_fix_tiers entry {t!r} not in {TIERS}")
        # Family-level consistency (the rules in protocol §5's table).
        fam = self.family
        if fam == "tierC_infeasible":
            if self.expected_tier != "C":
                errs.append("tierC_infeasible tasks must have expected_tier 'C'")
            if self.expected_terminal_outcome != "needs_human":
                errs.append("tierC_infeasible tasks must expect terminal outcome 'needs_human'")
            if self.expected_status == "OPTIMAL":
                errs.append("tierC_infeasible tasks cannot expect OPTIMAL")
        elif fam == "tierA_fixable":
            if self.expected_tier != "A":
                errs.append("tierA_fixable tasks must have expected_tier 'A'")
            if self.expected_terminal_outcome != "solved":
                errs.append("tierA_fixable tasks must expect terminal outcome 'solved'")
        elif fam == "tierB_fixable":
            if self.expected_tier != "B":
                errs.append("tierB_fixable tasks must have expected_tier 'B'")
            if self.expected_terminal_outcome != "solved":
                errs.append("tierB_fixable tasks must expect terminal outcome 'solved'")
        elif fam == "preflight_error":
            if self.expected_status != "ERROR" or self.expected_error_origin != "preflight":
                errs.append("preflight_error tasks must expect ERROR/preflight")
        elif fam == "output_anomaly":
            if self.expected_status not in (None, "OPTIMAL"):
                errs.append("output_anomaly tasks must expect OPTIMAL (the run solves; the outputs are wrong)")
            if self.planted_anomaly_metric is not None and self.expected_terminal_outcome != "flagged":
                errs.append("output_anomaly tasks with a planted metric must expect terminal outcome 'flagged'")
        if fam != "output_anomaly" and self.planted_anomaly_metric is not None:
            errs.append("planted_anomaly_metric is only meaningful for output_anomaly tasks")
        return errs


@dataclass
class Task:
    task_id: str
    adapter: str
    path: Path
    config: Dict[str, Any]
    label: TaskLabel
    prompt: Optional[str] = None

    @property
    def family(self) -> str:
        return self.label.family

    @property
    def needs_solver(self) -> str:
        return self.label.needs_solver

    def to_dict(self) -> Dict[str, Any]:
        return {
            "task_id": self.task_id,
            "adapter": self.adapter,
            "path": str(self.path),
            "config": self.config,
            "label": self.label.to_dict(),
            "prompt": self.prompt,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Task":
        return cls(
            task_id=data["task_id"],
            adapter=data["adapter"],
            path=Path(data.get("path", "")),
            config=data["config"],
            label=TaskLabel.from_dict(data["label"]),
            prompt=data.get("prompt"),
        )


# ---- filesystem ------------------------------------------------------------

def benchmark_root(adapter: str, parent: Union[str, Path, None] = None) -> Path:
    """``<parent>/<adapter>/benchmark`` (parent defaults to ``<repo>/examples``)."""
    parent = Path(parent) if parent is not None else DEFAULT_BENCHMARK_PARENT
    return parent / adapter / "benchmark"


def load_task(task_dir: Union[str, Path], adapter: Optional[str] = None) -> Task:
    task_dir = Path(task_dir)
    config = json.loads((task_dir / "config.json").read_text())
    expected = json.loads((task_dir / "expected.json").read_text())
    label = TaskLabel.from_dict(expected)
    prompt_path = task_dir / "prompt.md"
    prompt = prompt_path.read_text() if prompt_path.exists() else None
    if adapter is None:
        # examples/<adapter>/benchmark/<task_id>
        adapter = task_dir.resolve().parents[1].name
    return Task(task_id=label.task_id or task_dir.name, adapter=adapter,
                path=task_dir, config=config, label=label, prompt=prompt)


def discover_tasks(adapter: str, parent: Union[str, Path, None] = None) -> List[Task]:
    """Every task directory under the adapter's benchmark root, sorted by id."""
    root = benchmark_root(adapter, parent)
    if not root.is_dir():
        return []
    tasks = []
    for d in sorted(root.iterdir()):
        if d.is_dir() and (d / "config.json").exists() and (d / "expected.json").exists():
            tasks.append(load_task(d, adapter=adapter))
    return tasks


def select_tasks(tasks: Sequence[Task], spec: Union[str, Iterable[str], None]) -> List[Task]:
    """``spec`` is ``"all"``/``None``, a comma-separated id list, a family name,
    or an iterable of ids/families."""
    if spec is None or spec == "all":
        return list(tasks)
    wanted = [s.strip() for s in spec.split(",")] if isinstance(spec, str) else list(spec)
    wanted = [w for w in wanted if w]
    if not wanted or wanted == ["all"]:
        return list(tasks)
    by_id = {t.task_id: t for t in tasks}
    out: List[Task] = []
    for w in wanted:
        if w in by_id:
            out.append(by_id[w])
        elif w in FAMILIES:
            out.extend(t for t in tasks if t.family == w and t not in out)
        else:
            raise KeyError(f"unknown task or family {w!r}; known ids: {sorted(by_id)}")
    return out


def validate_task(task: Task) -> List[str]:
    errs = task.label.validate()
    if task.label.task_id and task.label.task_id != task.path.name and task.path.name:
        errs.append(f"task_id {task.label.task_id!r} != directory name {task.path.name!r}")
    if not isinstance(task.config, dict) or not task.config:
        errs.append("config.json must be a non-empty object")
    return errs


def write_task(
    task_dir: Union[str, Path],
    config: Dict[str, Any],
    label: Union[TaskLabel, Dict[str, Any]],
    prompt: Optional[str] = None,
) -> Path:
    """Materialise a task directory (used by tests and benchmark authoring)."""
    task_dir = Path(task_dir)
    task_dir.mkdir(parents=True, exist_ok=True)
    if isinstance(label, TaskLabel):
        label = label.to_dict()
    label = dict(label)
    label.setdefault("task_id", task_dir.name)
    # ensure_ascii=False: these files are read by humans as much as by the
    # harness, and the notes are full of en dashes and ± signs.
    (task_dir / "config.json").write_text(
        json.dumps(config, indent=2, ensure_ascii=False) + "\n")
    (task_dir / "expected.json").write_text(
        json.dumps(label, indent=2, ensure_ascii=False) + "\n")
    if prompt is not None:
        (task_dir / "prompt.md").write_text(prompt)
    return task_dir
