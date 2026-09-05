"""Remote execution backend for the Pathways model.

Same contract as :class:`~adapters.pathways.pathways_adapter.PathwaysAdapter`;
only ``run`` differs. It ships the config **and the driver + shim** to an
execution environment, runs them there, captures stdout into a local
``solver.log``, and pulls the run's result CSVs back. The framework, skills and
guardrail are unchanged — this is purely a different ``run()`` backend, and it
doubles as the "submit to a bigger box" piece. That matters more here than for
the reference model: a full 8760-hour Pathways year is a ~20 h solve on 32 cores.

The *mechanism* (ssh vs local loopback) is the same pluggable
:class:`~adapters.garuda.transport.Transport` the reference adapter uses. It is
imported **lazily**, inside the constructor, so this module (and the whole
``adapters.pathways`` package) still imports when the reference adapter is
absent.

Assumptions about the execution environment: a Python with gurobipy/pandas/
geopandas, a Gurobi licence, a checkout of the model at ``remote_model_root``,
and the Zenodo data at ``remote_data_root``. It does NOT provision any of those.

**Untested against a live node in this build.** The orchestration is exercised
with an in-memory transport in ``tests/test_pathways_adapter.py``; treat the ssh
path as unverified.
"""

from __future__ import annotations

import json
import shlex
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence

from framework.adapter import ValidationResult
from framework.run_record import Execution

from adapters.pathways import shim
from adapters.pathways.pathways_adapter import DRIVER_PATH, MODEL_REPO_URL, PathwaysAdapter


def _transport_module():
    """Import the transport layer lazily so a missing reference adapter cannot
    break ``import adapters.pathways``."""
    from adapters.garuda import transport  # noqa: PLC0415  (deliberately lazy)

    return transport


class RemotePathwaysAdapter(PathwaysAdapter):
    name = "pathways-remote"

    def __init__(
        self,
        remote_root: str,
        remote_model_root: str,
        remote_data_root: str,
        ssh_host: Optional[str] = None,
        transport: Optional[Any] = None,
        python: str = "python3",
        ssh_opts: Sequence[str] = (),
        repo_url: str = MODEL_REPO_URL,
        remote_env: Optional[Dict[str, str]] = None,
        model_root: Optional[str | Path] = None,
        data_root: Optional[str | Path] = None,
    ):
        # The local halves are only used for validation messages; the run
        # happens entirely on the other side.
        super().__init__(model_root=model_root or Path(remote_model_root),
                         data_root=data_root or remote_data_root,
                         python=python)
        if transport is None:
            mod = _transport_module()
            if ssh_host is None:
                raise ValueError("provide either ssh_host or an explicit transport")
            transport = mod.SshTransport(ssh_host, ssh_opts)
        self.transport = transport
        self.ssh_host = ssh_host
        self.remote_root = remote_root.rstrip("/")
        self.remote_model_root = remote_model_root.rstrip("/")
        self.remote_data_root = remote_data_root.rstrip("/")
        self.repo_url = repo_url
        self.remote_env: Dict[str, str] = dict(remote_env or {})

    @classmethod
    def local_loopback(cls, remote_root: str, remote_model_root: str,
                       remote_data_root: str, python: str = "python3",
                       **kwargs) -> "RemotePathwaysAdapter":
        """Run the remote orchestration on this machine (no ssh), for testing
        the backend before pointing it at a node."""
        mod = _transport_module()
        return cls(remote_root=remote_root, remote_model_root=remote_model_root,
                   remote_data_root=remote_data_root, transport=mod.LocalTransport(),
                   python=python, **kwargs)

    # ---- environment ----------------------------------------------------

    def _env_prefix(self) -> str:
        env = {"PATHWAYS_DATA_ROOT": self.remote_data_root,
               "PYTHONDONTWRITEBYTECODE": "1", **self.remote_env}
        return " ".join(f"{k}={shlex.quote(v)}" for k, v in env.items()) + " "

    def check_connection(self) -> Dict[str, Any]:
        """Probe the environment: host, interpreter, gurobipy, model, data."""
        py = shlex.quote(self.python)
        probe = (
            "echo HOST=$(hostname); "
            f"echo PYTHON=$(command -v {py} || echo MISSING); "
            f"echo GUROBIPY=$({py} -c 'import gurobipy;print(gurobipy.gurobi.version())' "
            "2>/dev/null || echo MISSING); "
            "echo GRB_LICENSE_FILE=${GRB_LICENSE_FILE:-unset}; "
            f"echo MODEL=$(test -f {shlex.quote(self.remote_model_root)}/pycode/main.py "
            "&& echo present || echo absent); "
            f"echo DATA=$(test -d {shlex.quote(self.remote_data_root)}/data_pkl "
            "&& echo present || echo absent)"
        )
        p = self.transport.run(probe)
        return {"returncode": p.returncode, "stdout": p.stdout, "stderr": p.stderr,
                "transport": self.transport.description}

    def clone_command(self) -> str:
        root = shlex.quote(self.remote_model_root)
        return (
            f"if [ ! -f {root}/pycode/main.py ]; then "
            f"mkdir -p $(dirname {root}) && git clone {shlex.quote(self.repo_url)} {root}; "
            f"else echo 'model present'; fi"
        )

    def ensure_remote_model(self):
        return self.transport.run(self.clone_command())

    # ---- adapter contract ------------------------------------------------

    def validate_config(self, config: Dict[str, Any]) -> ValidationResult:
        # Schema only: the model checkout and the data live on the other side,
        # where the driver's own workspace builder enforces their presence.
        errors: List[str] = self.validate_schema(config)
        return ValidationResult(ok=not errors, errors=errors)

    def run(
        self,
        config: Dict[str, Any],
        run_dir: Path,
        on_event: Optional[Callable[..., Any]] = None,
        on_line: Optional[Callable[[str], None]] = None,
    ) -> Execution:
        run_dir = Path(run_dir)
        run_dir.mkdir(parents=True, exist_ok=True)
        executed = self.executed_config(config, run_dir)
        job = f"{self.remote_root}/jobs/{shim.sanitize_tag(run_dir.name)}"
        log_path = run_dir / "solver.log"

        def _fail(stage: str, proc) -> Execution:
            log_path.write_text(
                f"REMOTE {stage} FAILED (transport={self.transport.description})\n"
                + (getattr(proc, "stderr", "") or "")
            )
            return Execution(termination_status="ERROR", solver_log="solver.log",
                             returncode=proc.returncode, error_origin="remote")

        # 1) ship the config plus the driver and its shim (the model itself and
        #    the data are expected to be there already).
        local_cfg = run_dir / "config.json"
        local_cfg.write_text(json.dumps(executed, indent=2))
        mk = self.transport.run(f"mkdir -p {shlex.quote(job)}")
        if mk.returncode != 0:
            return _fail("mkdir", mk)
        for local in (local_cfg, DRIVER_PATH, DRIVER_PATH.parent / "shim.py"):
            push = self.transport.push(str(local), f"{job}/{local.name}")
            if push.returncode != 0:
                return _fail(f"push {local.name}", push)

        # 2) run the driver there (captured, not streamed: the transport hands
        #    back a CompletedProcess).
        cmd = (
            f"cd {shlex.quote(job)} && {self._env_prefix()}{shlex.quote(self.python)} "
            f"{shlex.quote(job)}/driver.py --config {shlex.quote(job)}/config.json "
            f"--run-dir {shlex.quote(job)} "
            f"--model-root {shlex.quote(self.remote_model_root)} "
            f"--data-root {shlex.quote(self.remote_data_root)}"
        )
        start = time.monotonic()
        proc = self.transport.run(cmd)
        wall = time.monotonic() - start

        combined = (proc.stdout or "") + (
            "\n[stderr]\n" + proc.stderr if getattr(proc, "stderr", None) else ""
        )
        log_path.write_text(combined)
        if on_line is not None:
            for line in combined.splitlines(keepends=True):
                on_line(line)

        # 3) pull the run artefacts back: driver_result.json, the Gurobi log and
        #    the model's result CSVs.
        self.transport.pull(f"{job}/driver_result.json", str(run_dir / "driver_result.json"))
        ws = self.workspace_dir(run_dir)
        ws.mkdir(parents=True, exist_ok=True)
        self.transport.pull(f"{job}/workspace/gurobi.log", str(ws / "gurobi.log"))
        results_rel = shim.results_relpath(executed)
        local_results = ws / results_rel
        local_results.mkdir(parents=True, exist_ok=True)
        self.transport.pull(f"{job}/workspace/{results_rel}/", str(local_results) + "/")

        outcome = shim.parse_driver_output(
            combined, proc.returncode,
            gurobi_log=self.gurobi_log_text(run_dir),
            driver_result=self.read_driver_result(run_dir),
        )
        self.archive_outputs(executed, run_dir)
        return Execution(
            termination_status=outcome.status,
            wall_seconds=round(wall, 1),
            solver_log="solver.log",
            returncode=proc.returncode,
            error_origin=outcome.error_origin,
        )

    def results_dir(self, config: Dict[str, Any], run_dir: str | Path) -> Path:
        """Always the *local* mirror: ``driver_result.json`` records a path on
        the remote filesystem, which does not exist here."""
        return self.workspace_dir(run_dir) / shim.results_relpath(config)
