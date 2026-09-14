"""Small shared utilities for Julia models with fixed script entry points."""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, Optional

from framework.process import stream_command
from framework.run_record import Execution


def write_config(config: Dict[str, Any], run_dir: Path) -> Path:
    run_dir.mkdir(parents=True, exist_ok=True)
    path = run_dir / "config.json"
    path.write_text(json.dumps(config, indent=2, sort_keys=True) + "\n")
    return path


def archive_csvs(sources: Iterable[Path], run_dir: Path,
                 max_bytes: int = 20_000_000) -> Dict[str, Path]:
    """Copy bounded summary CSVs, excluding large dispatch tables."""
    output_dir = run_dir / "outputs"
    archived: Dict[str, Path] = {}
    for source in sources:
        if not source.is_file() or source.stat().st_size > max_bytes:
            continue
        if "dispatch" in source.name.lower() or "dispatch" in {
            part.lower() for part in source.parts
        }:
            continue
        output_dir.mkdir(parents=True, exist_ok=True)
        target = output_dir / source.name
        if target.exists():
            target = output_dir / f"{source.parent.name}_{source.name}"
        shutil.copy2(source, target)
        archived[target.stem] = target
    return archived


def output_map(run_dir: Path) -> Dict[str, Path]:
    output_dir = Path(run_dir) / "outputs"
    return ({path.stem: path for path in sorted(output_dir.glob("*.csv"))}
            if output_dir.is_dir() else {})


def run_julia(command: list[str], cwd: Path, run_dir: Path,
              on_event: Optional[Callable[..., Any]] = None,
              timeout: Optional[float] = None) -> tuple[Execution, str]:
    """Stream a Julia process and conservatively classify its outcome."""
    from framework.monitor import RunMonitor

    run_dir.mkdir(parents=True, exist_ok=True)
    monitor = RunMonitor(run_dir, on_event=on_event)
    lines: list[str] = []

    def on_line(line: str) -> None:
        lines.append(line)
        monitor.on_line(line)

    result = stream_command(
        command,
        cwd=cwd,
        log_path=run_dir / "solver.log",
        on_line=on_line,
        on_tick=monitor.tick,
        timeout=timeout,
        abort=monitor.abort_event,
        pid_file=None,
        abort_file=None,
    )
    text = "".join(lines)
    lowered = text.lower()
    if result.timed_out:
        status, origin = "TIME_LIMIT", "runtime"
    elif result.aborted:
        status, origin = "ABORTED", "runtime"
    elif result.returncode == 0 and "infeasible" not in lowered:
        status, origin = "OPTIMAL", None
    elif "infeasible" in lowered:
        status, origin = "INFEASIBLE", "solver"
    elif "time limit" in lowered or "time_limit" in lowered:
        status, origin = "TIME_LIMIT", "solver"
    else:
        status, origin = "ERROR", "runtime"
    summary = monitor.finish(result)
    return Execution(
        termination_status=status,
        wall_seconds=round(result.wall_seconds, 1),
        solver_log="solver.log",
        returncode=result.returncode,
        error_origin=origin,
        monitor=summary,
    ), text
