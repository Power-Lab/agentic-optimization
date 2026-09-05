"""``python -m framework.watch <run_dir> [--follow]`` — tail + summarise a run.

For a human or an agent looking at a solve that is still running (or just
finished). Replays ``<run_dir>/solver.log`` through a
:class:`~framework.monitor.LogWatcher`, prints a one-screen summary and the
last notable events, and with ``--follow`` keeps tailing the log (printing
events as they arrive, with stall / time-limit-near checks) until the run
ends or Ctrl-C. ``--json`` emits the summary as JSON for programmatic use.
``--abort [REASON]`` asks the run to stop (writes ``<run_dir>/ABORT``, which
``framework.process.stream_command`` polls, then signals ``run.pid`` if the
child ignores it).

Model-agnostic: it only reads the run dir's generic files (``solver.log``,
``monitor.json``, ``run_record.json``, ``run.pid``, ``config.json``).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, TextIO

from framework.monitor import NOTABLE_KINDS, TERMINAL_STATUSES, RunEvent, RunMonitor
from framework.process import request_abort


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def _read_pid(run_dir: Path) -> Optional[int]:
    try:
        return int((run_dir / "run.pid").read_text().strip())
    except (OSError, ValueError):
        return None


def _pid_alive(pid: Optional[int]) -> Optional[bool]:
    if pid is None:
        return None
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _load_json(path: Path) -> Optional[Dict[str, Any]]:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def _time_limit_hint(run_dir: Path, explicit: Optional[float]) -> Optional[float]:
    """The solver time limit, from the flag, the live monitor.json, or a config
    key literally named ``time_limit`` (a generic option name, not a model key)."""
    if explicit:
        return float(explicit)
    live = _load_json(run_dir / "monitor.json")
    if live and isinstance(live.get("time_limit"), (int, float)):
        return float(live["time_limit"])
    cfg = _load_json(run_dir / "config.json")
    if cfg and isinstance(cfg.get("time_limit"), (int, float)) and cfg["time_limit"] > 0:
        return float(cfg["time_limit"])
    return None


def _fmt(v: Any, digits: int = 6) -> str:
    if v is None:
        return "-"
    if isinstance(v, float):
        return f"{v:.{digits}g}"
    return str(v)


def _age(path: Path) -> Optional[float]:
    try:
        return max(0.0, time.time() - path.stat().st_mtime)
    except OSError:
        return None


class _Tail:
    """Incremental reader of a growing text file (a ``tail -f``)."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.pos = 0
        self.partial = ""

    def read_new_lines(self) -> List[str]:
        try:
            size = self.path.stat().st_size
        except OSError:
            return []
        if size < self.pos:           # truncated / rewritten: start over
            self.pos, self.partial = 0, ""
        if size == self.pos:
            return []
        with self.path.open("r", encoding="utf-8", errors="replace") as fh:
            fh.seek(self.pos)
            chunk = fh.read()
            self.pos = fh.tell()
        text = self.partial + chunk
        lines = text.split("\n")
        self.partial = lines.pop()  # incomplete last line (or "")
        return lines


# --------------------------------------------------------------------------
# rendering
# --------------------------------------------------------------------------

def render_summary(run_dir: Path, summary: Dict[str, Any], live: Optional[Dict[str, Any]],
                   record: Optional[Dict[str, Any]], n_lines: int) -> str:
    log = run_dir / "solver.log"
    pid = _read_pid(run_dir)
    alive = _pid_alive(pid)
    exec_ = (record or {}).get("execution") or {}
    lines: List[str] = []
    a = lines.append
    a(f"run:      {run_dir}")
    age = _age(log)
    size_kb = (log.stat().st_size / 1024.0) if log.is_file() else 0.0
    a(f"log:      {log.name} ({n_lines} lines, {size_kb:.1f} KB"
      + (f", last write {age:.0f}s ago)" if age is not None else ", missing)"))
    if pid is not None:
        a(f"process:  pid {pid} " + ("alive" if alive else "gone"))
    elif exec_:
        a(f"process:  finished (returncode {exec_.get('returncode')}, "
          f"wall {_fmt(exec_.get('wall_seconds'))}s, run_record status "
          f"{exec_.get('termination_status')})")
    else:
        a("process:  no run.pid and no run_record.json (not started, or the adapter "
          "does not stream)")
    solver = summary.get("solver") or "unknown solver"
    ver = summary.get("solver_version")
    kind = "MIP" if summary.get("is_mip") else "LP"
    a(f"solver:   {solver}{' ' + ver if ver else ''} | {kind} | phase: {summary.get('phase')}")
    status = summary.get("status")
    a(f"status:   {status or '(none yet)'}"
      + (f" (from {summary.get('status_source')})" if status else "")
      + (f" | error_origin: {summary.get('error_origin')}" if summary.get("error_origin") else ""))
    a(f"bounds:   incumbent {_fmt(summary.get('best_obj'))} | bound {_fmt(summary.get('bound'))}"
      f" | gap {_fmt(summary.get('gap_pct'), 4)}%"
      f" | nodes {_fmt(summary.get('nodes'))} | solver time {_fmt(summary.get('solver_time_s'))}s")
    ms = summary.get("model_size") or {}
    if ms:
        a(f"model:    {ms.get('rows')} rows x {ms.get('cols')} cols, {ms.get('nonzeros')} nz"
          + (f", {ms.get('integer')} integer" if ms.get("integer") else ""))
    tl = summary.get("time_limit")
    a(f"health:   warnings {summary.get('n_warnings', 0)} | errors {summary.get('n_errors', 0)}"
      f" | stalled {'YES (' + str(summary.get('stall_reason')) + ')' if summary.get('stalled') else 'no'}"
      f" | time_limit {_fmt(tl)}{' NEAR' if summary.get('time_limit_near') else ''}"
      f" | incumbents {summary.get('n_incumbents', 0)}")
    if live:
        a(f"live:     monitor.json updated {live.get('updated_at')} | phase {live.get('phase')}"
          f" | status {live.get('status')} | stalled {live.get('stalled')}"
          f" | aborted {live.get('aborted')}"
          + (f" ({live.get('abort_reason')})" if live.get("abort_reason") else ""))
    tb = summary.get("traceback")
    if tb:
        a(f"trace:    [{tb.get('language')}] {str(tb.get('message'))[:160]}")
    for w in list(summary.get("warnings") or [])[-3:]:
        a(f"warning:  {w[:160]}")
    for e in list(summary.get("errors") or [])[-3:]:
        a(f"error:    {e[:160]}")
    return "\n".join(lines)


def render_events(events: List[RunEvent], limit: int) -> str:
    notable = [e for e in events if e.kind in NOTABLE_KINDS]
    tail = notable[-limit:] if limit > 0 else []
    if not tail:
        return "events:   (none)"
    return "events (last %d notable):\n" % len(tail) + "\n".join(f"  {e}" for e in tail)


# --------------------------------------------------------------------------
# main modes
# --------------------------------------------------------------------------

def watch(run_dir: Path, follow: bool = False, interval: float = 2.0, n_events: int = 12,
          stall_seconds: Optional[float] = 600.0, time_limit: Optional[float] = None,
          as_json: bool = False, out: Optional[TextIO] = None,
          max_seconds: Optional[float] = None, quiet: bool = False) -> Dict[str, Any]:
    """Replay (and optionally follow) ``<run_dir>/solver.log``; return the summary."""
    out = out if out is not None else sys.stdout  # resolved per call (capturable)
    run_dir = Path(run_dir)
    log = run_dir / "solver.log"
    tl = _time_limit_hint(run_dir, time_limit)
    monitor = RunMonitor(run_dir=None, source="watch", persist_interval=float("inf"),
                         keep_events=max(50, n_events * 4),
                         stall_seconds=stall_seconds if follow else None, time_limit=tl)
    tail = _Tail(log)
    n_lines = 0

    def emit(ev: RunEvent) -> None:
        if follow and not as_json and not quiet and ev.kind in NOTABLE_KINDS:
            print(f"  {ev}", file=out, flush=True)

    monitor.on_event = None  # the initial replay is summarised, not streamed
    for line in tail.read_new_lines():
        n_lines += 1
        monitor.on_line(line)

    if follow:
        monitor.on_event = emit
        if not as_json and not quiet:
            print(f"following {log} (Ctrl-C to stop)", file=out, flush=True)
        t0 = time.monotonic()
        idle_polls_after_end = 0
        try:
            while True:
                new = tail.read_new_lines()
                for line in new:
                    n_lines += 1
                    monitor.on_line(line)
                if not new:
                    monitor.tick()
                pid = _read_pid(run_dir)
                alive = _pid_alive(pid)
                record_done = (run_dir / "run_record.json").is_file()
                status_done = monitor.watcher.status in TERMINAL_STATUSES
                if alive is False or (pid is None and (record_done or status_done)):
                    idle_polls_after_end = idle_polls_after_end + 1 if not new else 0
                    if idle_polls_after_end >= 2:
                        break
                if max_seconds is not None and time.monotonic() - t0 >= max_seconds:
                    break
                time.sleep(max(0.05, interval))
        except KeyboardInterrupt:
            if not as_json and not quiet:
                print("\nstopped following", file=out, flush=True)

    monitor.finish()  # flush an unterminated traceback; run_dir is None so nothing is written
    summary = monitor.summary()
    live = _load_json(run_dir / "monitor.json")
    record = _load_json(run_dir / "run_record.json")
    summary["run_dir"] = str(run_dir)
    summary["log_lines"] = n_lines
    summary["pid"] = _read_pid(run_dir)
    summary["pid_alive"] = _pid_alive(summary["pid"])
    summary["run_record_status"] = ((record or {}).get("execution") or {}).get("termination_status")
    summary["live_monitor"] = live
    if as_json:
        summary["recent_events"] = [e.to_dict() for e in list(monitor.events)
                                    if e.kind in NOTABLE_KINDS][-n_events:]
        print(json.dumps(summary, indent=2, default=str), file=out, flush=True)
    elif not quiet:
        print(render_summary(run_dir, summary, live, record, n_lines), file=out)
        print(render_events(list(monitor.events), n_events), file=out, flush=True)
    return summary


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(
        prog="python -m framework.watch",
        description="Tail and summarise an optimization run (solver.log) in a run dir.")
    p.add_argument("run_dir", help="the run directory (holds solver.log, run.pid, monitor.json)")
    p.add_argument("--follow", "-f", action="store_true",
                   help="keep tailing until the run ends or Ctrl-C")
    p.add_argument("--json", action="store_true", help="print the summary as JSON")
    p.add_argument("--events", type=int, default=12, help="how many recent notable events to show")
    p.add_argument("--interval", type=float, default=2.0, help="poll interval in seconds (--follow)")
    p.add_argument("--stall-seconds", type=float, default=600.0,
                   help="flag a stall after this many seconds without improvement/output (--follow)")
    p.add_argument("--time-limit", type=float, default=None,
                   help="solver time limit in seconds (else read from monitor.json / config.json)")
    p.add_argument("--max-seconds", type=float, default=None,
                   help="stop following after this long (for scripted use)")
    p.add_argument("--abort", nargs="?", const="requested via framework.watch", default=None,
                   metavar="REASON", help="ask the run to stop (writes ABORT, then signals run.pid)")
    p.add_argument("--quiet", "-q", action="store_true", help="print nothing but the JSON/summary")
    args = p.parse_args(argv)

    run_dir = Path(args.run_dir).expanduser().resolve()
    if args.abort is not None:
        info = request_abort(run_dir, reason=args.abort)
        if args.json:
            print(json.dumps(info, indent=2))
        else:
            print(f"abort file written: {info['abort_file']}")
            if info["pid"] is None:
                print("no run.pid found: the run is not streaming via stream_command (or has "
                      "already finished); nothing to signal")
            elif not info["alive_before"]:
                print(f"pid {info['pid']} is not running (already finished)")
            elif info["signalled"]:
                print(f"pid {info['pid']} ignored ABORT; sent SIGTERM to its process group "
                      f"(alive after: {info['alive_after']})")
            else:
                print(f"pid {info['pid']} stopped after the ABORT file")
        return 0

    if not (run_dir / "solver.log").is_file() and not args.follow:
        print(f"no solver.log in {run_dir}", file=sys.stderr)
        return 2
    watch(run_dir, follow=args.follow, interval=args.interval, n_events=args.events,
          stall_seconds=args.stall_seconds, time_limit=args.time_limit, as_json=args.json,
          max_seconds=args.max_seconds, quiet=args.quiet)
    return 0


if __name__ == "__main__":
    sys.exit(main())
