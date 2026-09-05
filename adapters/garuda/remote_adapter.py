"""Remote execution backend for the garuda model — the colo-node adapter.

Same contract as :class:`GarudaAdapter`; only ``run`` differs. It ships the
config to an execution environment, runs the solver there, captures stdout to a
local ``solver.log``, and pulls the result CSVs back. The framework, skills, and
guardrail are unchanged — this is purely a different ``run()`` backend, and it
doubles as the "submit to HPC" piece.

The *mechanism* (ssh vs local loopback) is a pluggable :class:`Transport`, so
the orchestration can be tested locally before pointing it at a real node.
Assumes the execution environment has Julia (+ a Gurobi licence if requested)
and a checkout of the model at ``remote_root`` (``ensure_remote_repo`` clones
it if not).

Untested against a live node in this build; the orchestration is exercised with
an in-memory transport in ``tests/test_garuda_adapter.py``.
"""

from __future__ import annotations

import json
import shlex
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence

from framework.adapter import ValidationResult
from framework.run_record import Execution
from adapters.garuda.transport import LocalTransport, SshTransport, Transport
from adapters.garuda.garuda_adapter import MODEL_REPO_URL, GarudaAdapter


class RemoteGarudaAdapter(GarudaAdapter):
    name = "garuda-remote"

    def __init__(
        self,
        remote_root: str,
        ssh_host: Optional[str] = None,
        transport: Optional[Transport] = None,
        julia: str = "julia",
        ssh_opts: Sequence[str] = (),
        repo_url: str = MODEL_REPO_URL,
        branch: Optional[str] = None,
        remote_env: Optional[Dict[str, str]] = None,
    ):
        super().__init__(julia=julia)
        # Default transport is ssh when a host is given; callers can inject a
        # LocalTransport for loopback testing.
        if transport is None:
            if ssh_host is None:
                raise ValueError("provide either ssh_host or an explicit transport")
            transport = SshTransport(ssh_host, ssh_opts)
        self.transport = transport
        self.ssh_host = ssh_host
        self.remote_root = remote_root.rstrip("/")
        self.repo_url = repo_url
        self.branch = branch
        # e.g. {"GARUDA_SKIP_VALIDATION": "1"} on a node without pandas.
        self.remote_env: Dict[str, str] = dict(remote_env or {})

    @classmethod
    def local_loopback(cls, remote_root: str, julia: str = "julia",
                       remote_env: Optional[Dict[str, str]] = None) -> "RemoteGarudaAdapter":
        """Construct an adapter that runs the remote orchestration on this
        machine (no ssh). For testing the backend before a real node."""
        return cls(remote_root=remote_root, transport=LocalTransport(), julia=julia,
                   remote_env=remote_env)

    # ---- paths ---------------------------------------------------------

    def _remote_results_dir(self, config: Dict[str, Any]) -> str:
        return f"{self.remote_root}/results/{self.results_name(config)}"

    def _env_prefix(self) -> str:
        if not self.remote_env:
            return ""
        return " ".join(f"{k}={shlex.quote(v)}" for k, v in self.remote_env.items()) + " "

    # ---- setup / health -----------------------------------------------

    def check_connection(self) -> Dict[str, Any]:
        """Probe the environment: reachability, julia, Gurobi licence, repo."""
        probe = (
            "echo HOST=$(hostname); "
            "echo JULIA=$(command -v julia || echo MISSING); "
            "echo GRB_LICENSE_FILE=${GRB_LICENSE_FILE:-unset}; "
            f"echo REPO=$(test -f {shlex.quote(self.remote_root)}/run_model.jl "
            "&& echo present || echo absent)"
        )
        p = self.transport.run(probe)
        return {"returncode": p.returncode, "stdout": p.stdout, "stderr": p.stderr,
                "transport": self.transport.description}

    def clone_command(self) -> str:
        root = shlex.quote(self.remote_root)
        branch = f"-b {shlex.quote(self.branch)} " if self.branch else ""
        return (
            f"if [ ! -f {root}/run_model.jl ]; then "
            f"mkdir -p $(dirname {root}) && "
            f"git clone {branch}{shlex.quote(self.repo_url)} {root} && "
            f"{self.julia} --project={root} {root}/bootstrap.jl; "
            f"else echo 'repo present'; fi"
        )

    def ensure_remote_repo(self):
        """Clone + bootstrap the model in the environment if it isn't present."""
        return self.transport.run(self.clone_command())

    # ---- adapter interface --------------------------------------------

    def validate_config(self, config: Dict[str, Any]) -> ValidationResult:
        # Schema-only locally; the input-folder existence lives in the execution
        # environment and is enforced there by the model's own preflight.
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
        name = self.results_name(executed)
        remote_job = f"{self.remote_root}/jobs/{name}"
        remote_cfg = f"{remote_job}/config.json"
        log_path = run_dir / "solver.log"

        def _fail(stage: str, proc) -> Execution:
            log_path.write_text(f"REMOTE {stage} FAILED (transport={self.transport.description})\n"
                                + (proc.stderr or ""))
            return Execution(termination_status="ERROR", solver_log="solver.log",
                             returncode=proc.returncode, error_origin="remote")

        # 1) ship the config (the executed one, run_tag included)
        local_cfg = run_dir / "config.json"
        local_cfg.write_text(json.dumps(executed, indent=2))
        mk = self.transport.run(f"mkdir -p {shlex.quote(remote_job)}")
        if mk.returncode != 0:
            return _fail("mkdir", mk)
        push = self.transport.push(str(local_cfg), remote_cfg)
        if push.returncode != 0:
            return _fail("config push", push)

        # 2) run the solver in the execution environment (captured, not streamed:
        #    the transport returns a CompletedProcess)
        run_cmd = (
            f"cd {shlex.quote(self.remote_root)} && "
            f"{self._env_prefix()}{self.julia} --project={shlex.quote(self.remote_root)} "
            f"run_model.jl --config {shlex.quote(remote_cfg)}"
        )
        start = time.monotonic()
        proc = self.transport.run(run_cmd)
        wall = time.monotonic() - start

        combined = (proc.stdout or "") + ("\n[stderr]\n" + proc.stderr if proc.stderr else "")
        log_path.write_text(combined)
        if on_line is not None:
            for line in combined.splitlines(keepends=True):
                on_line(line)
        status, origin = self._parse_status(combined, proc.returncode)

        # 3) pull results back into the local run dir
        out_dir = run_dir / "outputs"
        out_dir.mkdir(parents=True, exist_ok=True)
        self.transport.pull(self._remote_results_dir(executed) + "/", str(out_dir) + "/")

        return Execution(
            termination_status=status,
            wall_seconds=round(wall, 1),
            mipgap_reached=self._parse_gap(combined),
            solver_log="solver.log",
            returncode=proc.returncode,
            error_origin=origin,
        )
