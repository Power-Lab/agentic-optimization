"""The inter-skill contract: ``run_record.json``.

A run record is the *only* artifact the skills pass to one another. It captures
one optimization run's full state and history, which makes every skill
independently testable and the whole refine loop auditable.

The schema is deliberately model-agnostic: nothing here knows any model's
regions, keys, or file names. A model plugs in through ``framework.adapter.Adapter``;
the record just stores whatever config dict and metrics the adapter produces.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

SCHEMA_VERSION = 1


def config_hash(config: Dict[str, Any]) -> str:
    """Stable hash of a config dict — the supervisor's dedup key.

    Keys are sorted so two configs that differ only in key order hash equal.
    """
    blob = json.dumps(config, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


@dataclass
class Execution:
    """What the model-runner observed when it ran the model."""

    termination_status: Optional[str] = None  # OPTIMAL | TIME_LIMIT | INFEASIBLE | ERROR | ...
    wall_seconds: Optional[float] = None
    mipgap_reached: Optional[float] = None
    solver_log: Optional[str] = None  # path to captured solver.log, relative to run dir
    returncode: Optional[int] = None
    error_origin: Optional[str] = None  # "preflight" | "solver" | "runtime" | None
    # Rolling summary from the live run monitor (framework.monitor.RunMonitor
    # .summary()): phase, best_obj, bound, last_gap, warnings, stalled, ...
    # None for records written before monitoring existed or by adapters that
    # do not stream. Optional so old run_record.json files still load.
    monitor: Optional[Dict[str, Any]] = None


@dataclass
class Diagnosis:
    """What the log-analyzer concluded from the solver log + termination status."""

    status: Optional[str] = None  # mirrors/refines Execution.termination_status
    root_cause: Optional[str] = None
    evidence: List[str] = field(default_factory=list)  # e.g. ["log:412", "log:418"]
    # which intervention tier the analyzer believes is needed: "A" | "B" | "C" | None
    suggested_intervention_tier: Optional[str] = None
    confidence: Optional[float] = None  # 0..1


@dataclass
class Anomaly:
    """One flagged output pattern from the output-analyzer."""

    metric: str
    value: Any
    expected: str
    severity: str  # "low" | "medium" | "high"


@dataclass
class Refinement:
    """One controlled change applied by the refiner. The audit trail entry."""

    tier: str  # "A" | "B" | "C"
    change: Dict[str, Any]  # {key: [before, after]}
    rationale: str
    applied: bool  # False when Tier C is blocked pending human sign-off
    applied_by: str = "refiner"  # "refiner-unguarded" in the ablation condition
    # Did the proposer explicitly flag this change to a human? None when the
    # proposer did not say (plain ProposedChange). Lets the eval split silent
    # from disclosed policy relaxations.
    disclosed: Optional[bool] = None


@dataclass
class RunRecord:
    """One optimization run plus everything the skills learned about it."""

    config: Dict[str, Any]
    config_hash: str = ""
    schema_version: int = SCHEMA_VERSION
    execution: Execution = field(default_factory=Execution)
    log_diagnosis: Optional[Diagnosis] = None
    output_anomalies: List[Anomaly] = field(default_factory=list)
    refinement_history: List[Refinement] = field(default_factory=list)
    parent_run: Optional[str] = None  # config_hash of the run this was refined from

    def __post_init__(self) -> None:
        if not self.config_hash:
            self.config_hash = config_hash(self.config)

    # ---- serialization -------------------------------------------------

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    def save(self, path: str | Path) -> Path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.to_dict(), indent=2))
        return path

    @classmethod
    def load(cls, path: str | Path) -> "RunRecord":
        data = json.loads(Path(path).read_text())
        return cls.from_dict(data)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "RunRecord":
        execution = Execution(**data.get("execution", {}))
        diag_data = data.get("log_diagnosis")
        diagnosis = Diagnosis(**diag_data) if diag_data else None
        anomalies = [Anomaly(**a) for a in data.get("output_anomalies", [])]
        refinements = [Refinement(**r) for r in data.get("refinement_history", [])]
        return cls(
            config=data["config"],
            config_hash=data.get("config_hash", ""),
            schema_version=data.get("schema_version", SCHEMA_VERSION),
            execution=execution,
            log_diagnosis=diagnosis,
            output_anomalies=anomalies,
            refinement_history=refinements,
            parent_run=data.get("parent_run"),
        )
