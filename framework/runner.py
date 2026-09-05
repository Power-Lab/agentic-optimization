"""Glue that turns one adapter run into a saved run record.

Used by the model-runner skill. Kept model-agnostic: it only touches the
adapter interface and the run-record contract.

Live monitoring: pass ``on_event`` to receive :class:`framework.monitor.RunEvent`
objects while the solve runs. Adapters whose ``run`` accepts ``on_event`` get
it; for every adapter, ``Execution.monitor`` ends up holding a rolling summary
(from the adapter's live monitor, or from replaying ``solver.log`` afterwards)
and ``<run_dir>/monitor.json`` is written so ``python -m framework.watch`` can
inspect the run.
"""

from __future__ import annotations

import inspect
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from framework.adapter import Adapter
from framework.run_record import Execution, RunRecord


def adapter_accepts_on_event(adapter: Adapter) -> bool:
    """Does ``adapter.run`` take an ``on_event`` keyword (or ``**kwargs``)?"""
    try:
        params = inspect.signature(adapter.run).parameters
    except (TypeError, ValueError):
        return False
    if "on_event" in params:
        return True
    return any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values())


class _Listener:
    """Wraps the caller's ``on_event`` (or nothing) and counts deliveries, so
    the runner knows whether the adapter fired events live before it decides
    to replay ``solver.log`` for the listener."""

    def __init__(self, on_event: Optional[Callable[[Any], None]]) -> None:
        self.on_event = on_event
        self.n_delivered = 0

    def __call__(self, event: Any) -> None:
        self.n_delivered += 1
        if self.on_event is not None:
            self.on_event(event)


def _call_run(adapter: Adapter, config: Dict[str, Any], run_dir: Path,
              listener: Optional[_Listener]) -> Execution:
    if listener is not None:
        return adapter.run(config, run_dir, on_event=listener)
    return adapter.run(config, run_dir)


def attach_monitor_summary(execution: Execution, run_dir: Path,
                           on_event: Optional[Callable[[Any], None]] = None) -> Execution:
    """Fill ``execution.monitor`` (and ``mipgap_reached`` when unknown) by
    replaying ``solver.log`` — for adapters that did not monitor live — and
    write ``<run_dir>/monitor.json`` stamped with the process result.

    Never raises: monitoring is advisory and must not break a run record.
    When the adapter did not fire live events, the replayed events are
    delivered to ``on_event`` so a listener still sees what happened.
    """
    if execution.monitor is not None:
        gap = execution.monitor.get("last_gap") if isinstance(execution.monitor, dict) else None
        if execution.mipgap_reached is None and isinstance(gap, (int, float)):
            execution.mipgap_reached = float(gap)
        return execution
    log_name = execution.solver_log or "solver.log"
    log_path = run_dir / log_name
    if not log_path.is_file():
        return execution
    try:
        from framework.monitor import replay_log

        monitor = replay_log(log_path, on_event=on_event, run_dir=None, stall_seconds=None)
        monitor.returncode = execution.returncode
        monitor.wall_seconds = execution.wall_seconds
        monitor.run_dir = run_dir
        try:
            monitor.save()
        except OSError:
            pass
        summary = monitor.summary()
        execution.monitor = summary
        gap = summary.get("last_gap")
        if execution.mipgap_reached is None and isinstance(gap, (int, float)):
            execution.mipgap_reached = float(gap)
    except Exception:  # noqa: BLE001 - advisory only
        pass
    return execution


def run_and_record(
    adapter: Adapter,
    config: Dict[str, Any],
    run_dir: str | Path,
    parent_run: Optional[str] = None,
    preflight: bool = True,
    on_event: Optional[Callable[[Any], None]] = None,
) -> RunRecord:
    """Run one config through the adapter and persist a ``run_record.json``.

    The record is written even when the run fails (infeasible, preflight error),
    because a failed run is exactly what the log-analyzer and refiner act on.

    ``on_event(event)`` — optional listener for live :class:`RunEvent` objects
    (see ``framework.monitor``). Adapters whose ``run`` accepts ``on_event``
    always get a listener (a no-op one when the caller passed none), so a
    streaming adapter builds its live monitor and keeps ``monitor.json``
    current while the solve runs; for the other adapters the events are
    replayed from ``solver.log`` once the run has finished. Either way the
    caller's listener sees each event exactly once.
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

    listener = _Listener(on_event) if adapter_accepts_on_event(adapter) else None
    execution = _call_run(adapter, config, run_dir, listener)
    # Replay only reaches the caller's listener when nothing was delivered live.
    delivered_live = listener is not None and listener.n_delivered > 0
    record.execution = attach_monitor_summary(execution, run_dir,
                                              on_event=None if delivered_live else on_event)
    record.save(run_dir / "run_record.json")
    return record
