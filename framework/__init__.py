"""Model-agnostic agentic supervisory framework for iterative optimization."""

from framework.adapter import Adapter, ValidationResult
from framework.agent_driver import (
    AgentDriver,
    Proposal,
    ProposalSet,
    make_analyze_fn,
    make_propose_fn,
)
from framework.analyze import read_outputs, record_anomalies
from framework.interventions import (
    Decision,
    InterventionSpec,
    TransitionRule,
    ProposedChange,
    Tier,
    decide,
)
from framework.llm import (
    AnthropicAPIClient,
    ClaudeCLIClient,
    FakeLLMClient,
    LLMClient,
    LLMError,
    available_clients,
    make_client,
    register_client,
)
from framework.monitor import LogWatcher, RunEvent, RunMonitor, replay_log
from framework.process import StreamResult, stream_command
from framework.refine import (
    APPLIED_BY_GUARDED,
    APPLIED_BY_UNGUARDED,
    RefineOutcome,
    apply_refinements,
)
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
    "TransitionRule",
    "ProposedChange",
    "Decision",
    "decide",
    "RefineOutcome",
    "apply_refinements",
    "APPLIED_BY_GUARDED",
    "APPLIED_BY_UNGUARDED",
    # output analysis
    "read_outputs",
    "record_anomalies",
    # supervisor
    "Supervisor",
    "StopCriteria",
    "SupervisorResult",
    # live monitoring
    "stream_command",
    "StreamResult",
    "LogWatcher",
    "RunMonitor",
    "RunEvent",
    "replay_log",
    # headless driver / provider seam
    "LLMClient",
    "LLMError",
    "ClaudeCLIClient",
    "FakeLLMClient",
    "AnthropicAPIClient",
    "make_client",
    "register_client",
    "available_clients",
    "AgentDriver",
    "Proposal",
    "ProposalSet",
    "make_propose_fn",
    "make_analyze_fn",
]
