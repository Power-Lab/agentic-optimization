"""Streamed subprocess execution for model adapters.

:func:`stream_command` runs a solver / model wrapper as a child process, tees
its merged stdout+stderr into a log file *as each line arrives*, hands every
line to an ``on_line`` callback (typically :meth:`framework.monitor.RunMonitor.on_line`),
and returns a :class:`StreamResult`. It supports a wall-clock ``timeout``, a
cooperative abort (a :class:`threading.Event` and/or an ``ABORT`` file in the
run dir, which ``python -m framework.watch <run_dir> --abort`` writes), a
periodic ``on_tick`` callback for time-based checks (stall detection), and a
``run.pid`` file so an external watcher can find the child.

Model-agnostic: it knows nothing about what it runs.

Typical adapter wiring::

    from framework.monitor import RunMonitor
    from framework.process import stream_command

    monitor = RunMonitor(run_dir, on_event=on_event, time_limit=config.get("time_limit"))
    result = stream_command(cmd, cwd=model_root, log_path=run_dir / "solver.log",
                            env=env, on_line=monitor.on_line, on_tick=monitor.tick,
                            abort=monitor.abort_event)
    summary = monitor.finish(result)

Lines are delivered to ``on_line`` exactly as read (trailing newline kept), so
``"".join(lines)`` reproduces the log; :class:`~framework.monitor.LogWatcher`
strips them itself.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Sequence, Union

PathLike = Union[str, "os.PathLike[str]"]

#: How often the main loop polls the child / abort flag / timeout (seconds).
POLL_INTERVAL = 0.05


@dataclass
class StreamResult:
    """What :func:`stream_command` observed."""

    returncode: Optional[int]
    wall_seconds: float
    timed_out: bool = False
    aborted: bool = False
    abort_reason: Optional[str] = None
    n_lines: int = 0
    log_path: Optional[str] = None
    callback_errors: int = 0   # exceptions raised by on_line / on_tick (swallowed)

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and not self.timed_out and not self.aborted


def _resolve_side_file(value: Any, run_dir: Optional[Path], default_name: str) -> Optional[Path]:
    """``None`` -> ``<run_dir>/<default_name>`` when a run dir is known,
    ``False`` -> disabled, otherwise the given path."""
    if value is False:
        return None
    if value is None:
        return (run_dir / default_name) if run_dir is not None else None
    return Path(value)


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _signal_process(proc: subprocess.Popen, sig: int) -> None:
    """Signal the child's whole process group when we own one (POSIX), else the child."""
    if proc.poll() is not None:
        return
    try:
        if os.name == "posix":
            os.killpg(os.getpgid(proc.pid), sig)
        else:  # pragma: no cover - Windows
            if sig == signal.SIGTERM:
                proc.terminate()
            else:
                proc.kill()
    except (ProcessLookupError, PermissionError, OSError):
        try:
            proc.terminate() if sig == signal.SIGTERM else proc.kill()
        except OSError:
            pass


def terminate_process(proc: subprocess.Popen, grace_seconds: float = 10.0) -> None:
    """SIGTERM the child (and its process group), then SIGKILL after ``grace_seconds``."""
    if proc.poll() is not None:
        return
    _signal_process(proc, signal.SIGTERM)
    try:
        proc.wait(timeout=max(0.0, grace_seconds))
    except subprocess.TimeoutExpired:
        _signal_process(proc, signal.SIGKILL)
        try:
            proc.wait(timeout=5.0)
        except subprocess.TimeoutExpired:  # pragma: no cover - unkillable child
            pass


def stream_command(
    cmd: Sequence[Any],
    cwd: Optional[PathLike] = None,
    log_path: Optional[PathLike] = None,
    env: Optional[Dict[str, str]] = None,
    on_line: Optional[Callable[[str], Any]] = None,
    timeout: Optional[float] = None,
    abort: Optional[threading.Event] = None,
    *,
    on_tick: Optional[Callable[[], Any]] = None,
    tick_interval: float = 1.0,
    pid_file: Any = None,
    abort_file: Any = None,
    grace_seconds: float = 10.0,
    append: bool = False,
    encoding: str = "utf-8",
) -> StreamResult:
    """Run ``cmd``; tee merged stdout/stderr into ``log_path`` line by line.

    Parameters
    ----------
    cmd:
        Argument list (``Path`` parts are accepted). No shell.
    cwd, env:
        Passed to :class:`subprocess.Popen`.
    log_path:
        File that receives every output line as it arrives (flushed per line).
        ``None`` keeps no file.
    on_line:
        Called with each line (trailing newline kept) from the reader thread.
        Exceptions are swallowed and counted in ``callback_errors`` so a
        monitoring bug can never break the run.
    timeout:
        Wall-clock seconds after which the child is terminated
        (``timed_out=True``). ``None`` = no limit.
    abort:
        A :class:`threading.Event`; when set, the child is terminated
        (``aborted=True``). One is created internally when ``None``.
    on_tick, tick_interval:
        ``on_tick()`` is called from the main thread about every
        ``tick_interval`` seconds while the child runs (stall / time-limit
        checks in :class:`~framework.monitor.RunMonitor`).
    pid_file, abort_file:
        Side files for external watchers. ``None`` (default) means
        ``<log dir>/run.pid`` and ``<log dir>/ABORT`` when ``log_path`` is
        given; ``False`` disables; a path overrides. A stale ``ABORT`` file
        is removed at start; the ``pid`` file is removed at exit. Creating the
        abort file while the child runs aborts it (its first line is the
        reason).
    grace_seconds:
        Time between SIGTERM and SIGKILL when terminating.
    append:
        Append to ``log_path`` instead of truncating it.
    """
    argv = [os.fspath(c) if isinstance(c, os.PathLike) else str(c) for c in cmd]
    log = Path(log_path) if log_path is not None else None
    run_dir = log.parent if log is not None else None
    pid_path = _resolve_side_file(pid_file, run_dir, "run.pid")
    abort_path = _resolve_side_file(abort_file, run_dir, "ABORT")
    abort_event = abort if abort is not None else threading.Event()

    if log is not None:
        log.parent.mkdir(parents=True, exist_ok=True)
    if abort_path is not None:
        try:
            if abort_path.exists():
                abort_path.unlink()
        except OSError:
            pass

    popen_kwargs: Dict[str, Any] = dict(
        cwd=None if cwd is None else os.fspath(cwd), env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1,
        encoding=encoding, errors="replace",
    )
    if os.name == "posix":
        popen_kwargs["start_new_session"] = True  # own process group -> killpg works

    start = time.monotonic()
    proc = subprocess.Popen(argv, **popen_kwargs)
    assert proc.stdout is not None

    if pid_path is not None:
        try:
            pid_path.parent.mkdir(parents=True, exist_ok=True)
            pid_path.write_text(f"{proc.pid}\n")
        except OSError:
            pid_path = None

    state: Dict[str, Any] = {"n_lines": 0, "callback_errors": 0}
    log_fh = log.open("a" if append else "w", encoding=encoding, errors="replace") if log else None

    def _reader() -> None:
        try:
            for line in iter(proc.stdout.readline, ""):
                if log_fh is not None:
                    log_fh.write(line)
                    log_fh.flush()
                state["n_lines"] += 1
                if on_line is not None:
                    try:
                        on_line(line)
                    except Exception:  # noqa: BLE001 - monitoring must not break the run
                        state["callback_errors"] += 1
        except ValueError:  # stdout closed underneath us
            pass

    reader = threading.Thread(target=_reader, name="stream_command-reader", daemon=True)
    reader.start()

    timed_out = False
    aborted = False
    abort_reason: Optional[str] = None
    next_tick = start + tick_interval

    def _abort_file_reason() -> Optional[str]:
        if abort_path is None:
            return None
        try:
            if not abort_path.exists():
                return None
            text = abort_path.read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            return None
        first = text.splitlines()[0] if text else ""
        return first or "abort file"

    try:
        while proc.poll() is None:
            now = time.monotonic()
            if abort_event.is_set():
                aborted, abort_reason = True, "abort event"
                terminate_process(proc, grace_seconds)
                break
            reason = _abort_file_reason()
            if reason is not None:
                aborted, abort_reason = True, reason
                abort_event.set()
                terminate_process(proc, grace_seconds)
                break
            if timeout is not None and (now - start) >= timeout:
                timed_out = True
                terminate_process(proc, grace_seconds)
                break
            if on_tick is not None and now >= next_tick:
                try:
                    on_tick()
                except Exception:  # noqa: BLE001
                    state["callback_errors"] += 1
                next_tick = time.monotonic() + tick_interval
            time.sleep(POLL_INTERVAL)
    except KeyboardInterrupt:
        terminate_process(proc, grace_seconds)
        aborted, abort_reason = True, "keyboard interrupt"
        abort_event.set()
        raise
    finally:
        returncode = proc.wait()
        # Grandchildren may hold the pipe open after the child died; do not
        # hang forever on them (killpg above normally takes them down too).
        reader.join(timeout=max(1.0, grace_seconds))
        if reader.is_alive():
            # The reader is still parked inside readline() because a grandchild
            # kept the write end open. It holds the BufferedReader's lock, so
            # closing the pipe here would block the main thread forever (the
            # join timeout above exists precisely to avoid hanging). Leave the
            # pipe to the daemon thread and to interpreter shutdown.
            pass
        else:
            try:
                proc.stdout.close()
            except OSError:
                pass
        if log_fh is not None:
            log_fh.close()
        if pid_path is not None:
            try:
                pid_path.unlink()
            except OSError:
                pass

    return StreamResult(
        returncode=returncode,
        wall_seconds=round(time.monotonic() - start, 3),
        timed_out=timed_out,
        aborted=aborted,
        abort_reason=abort_reason,
        n_lines=int(state["n_lines"]),
        log_path=None if log is None else str(log),
        callback_errors=int(state["callback_errors"]),
    )


def request_abort(run_dir: PathLike, reason: str = "requested", grace_seconds: float = 5.0,
                  signal_pid: bool = True) -> Dict[str, Any]:
    """Ask the run in ``run_dir`` to stop, from *outside* its process.

    Writes ``<run_dir>/ABORT`` (which :func:`stream_command` polls), then — if
    ``<run_dir>/run.pid`` names a live process that has not exited within
    ``grace_seconds`` — sends SIGTERM to its process group. Returns what was
    done: ``{"abort_file", "pid", "alive_before", "signalled", "alive_after"}``.
    """
    run_dir = Path(run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)
    abort_path = run_dir / "ABORT"
    abort_path.write_text(f"{reason}\n{time.strftime('%Y-%m-%dT%H:%M:%S')}\n")
    info: Dict[str, Any] = {"abort_file": str(abort_path), "pid": None, "alive_before": False,
                            "signalled": False, "alive_after": False}
    pid_path = run_dir / "run.pid"
    pid: Optional[int] = None
    try:
        pid = int(pid_path.read_text().strip())
    except (OSError, ValueError):
        return info
    info["pid"] = pid
    info["alive_before"] = _pid_alive(pid)
    if not info["alive_before"]:
        return info
    deadline = time.monotonic() + max(0.0, grace_seconds)
    while time.monotonic() < deadline and _pid_alive(pid):
        time.sleep(0.1)
    if _pid_alive(pid) and signal_pid and os.name == "posix":
        try:
            os.killpg(os.getpgid(pid), signal.SIGTERM)
        except (ProcessLookupError, PermissionError, OSError):
            try:
                os.kill(pid, signal.SIGTERM)
            except OSError:
                pass
        info["signalled"] = True
        time.sleep(0.2)
    info["alive_after"] = _pid_alive(pid)
    return info


def python_child(code: str) -> list:
    """``[sys.executable, "-u", "-c", code]`` — handy for tests and examples."""
    return [sys.executable, "-u", "-c", code]
