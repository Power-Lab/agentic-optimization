"""Evaluation harness for the guardrail study (docs/EXPERIMENTAL_PROTOCOL.md §4–8).

The package is adapter-agnostic: a benchmark task lives under
``examples/<adapter>/benchmark/<task_id>/`` (protocol §5), a *cell* is one
(task × adapter × LLM × guardrail × seed) combination (§4), ``run_cell`` drives
the supervisor loop through the headless agent driver and persists the
run-record chain under ``eval_runs/`` (§8), and ``scoring``/``report`` compute
the §6 metrics mechanically from those records.

Command line: ``python -m eval --help``.
"""

from eval.cache import CachedAdapter, SolveCache
from eval.cells import (
    Cell,
    CellResult,
    EvalSetupError,
    attach_user_context,
    plan_cells,
    run_cell,
    run_cells,
)
from eval.report import gvr_figure, write_report
from eval.scoring import (
    CODEBOOK,
    CellScore,
    KeywordRater,
    aggregate,
    score,
    score_cells,
    write_scores,
)
from eval.tasks import (
    FAMILIES,
    Task,
    TaskLabel,
    benchmark_root,
    discover_tasks,
    load_task,
    select_tasks,
    validate_task,
    write_task,
)

__all__ = [
    "FAMILIES", "Task", "TaskLabel", "benchmark_root", "discover_tasks",
    "load_task", "select_tasks", "validate_task", "write_task",
    "Cell", "CellResult", "EvalSetupError", "attach_user_context",
    "plan_cells", "run_cell", "run_cells",
    "SolveCache", "CachedAdapter",
    "CODEBOOK", "CellScore", "KeywordRater", "aggregate", "score", "score_cells",
    "write_scores",
    "write_report", "gvr_figure",
]
