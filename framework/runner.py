"""Glue that turns one adapter run into a saved run record.

Used by the model-runner skill. Kept model-agnostic: it only touches the
adapter interface and the run-record contract.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Optional

from framework.adapter import Adapter
from framework.run_record import RunRecord


def run_and_record(
    adapter: Adapter,
    config: Dict[str, Any],
    run_dir: str | Path,
    parent_run: Optional[str] = None,
    preflight: bool = True,
) -> RunRecord:
    """Run one config through the adapter and persist a ``run_record.json``.

    The record is written even when the run fails (infeasible, preflight error),
    because a failed run is exactly what the log-analyzer and refiner act on.
    """
    run_dir = Path(run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)

    record = RunRecord(config=config, parent_run=parent_run)

    if preflight:
        result = adapter.validate_config(config)
        if not result.ok:
            record.execution.termination_status = "ERROR"
            record.execution.error_origin = "preflight"
            record.execution.returncode = None
            # Stash the preflight errors where the analyzer can read them.
            (run_dir / "solver.log").write_text(
                "PREFLIGHT FAILED\n" + "\n".join(result.errors) + "\n"
            )
            record.execution.solver_log = "solver.log"
            record.save(run_dir / "run_record.json")
            return record

    execution = adapter.run(config, run_dir)
    record.execution = execution
    record.save(run_dir / "run_record.json")
    return record
