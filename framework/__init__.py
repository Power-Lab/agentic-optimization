"""Model-agnostic agentic supervisory framework for iterative optimization."""

from framework.adapter import Adapter, ValidationResult
from framework.analyze import read_outputs, record_anomalies
from framework.interventions import (
    Decision,
    InterventionSpec,
    ProposedChange,
    Tier,
    decide,
)
from framework.refine import RefineOutcome, apply_refinements
from framework.registry import available_adapters, get_adapter, register_adapter
from framework.run_record import (
    Anomaly,
    Diagnosis,
    Execution,
    Refinement,
    RunRecord,
    config_hash,
)
from framework.runner import run_and_record
from framework.supervisor import StopCriteria, Supervisor, SupervisorResult

__all__ = [
    # core contract + adapter
    "Adapter",
    "ValidationResult",
    "RunRecord",
    "Execution",
    "Diagnosis",
    "Anomaly",
    "Refinement",
    "config_hash",
    "run_and_record",
    # registry
    "get_adapter",
    "register_adapter",
    "available_adapters",
    # guardrail
    "Tier",
    "InterventionSpec",
    "ProposedChange",
    "Decision",
    "decide",
    "RefineOutcome",
    "apply_refinements",
    # output analysis
    "read_outputs",
    "record_anomalies",
    # supervisor
    "Supervisor",
    "StopCriteria",
    "SupervisorResult",
]
