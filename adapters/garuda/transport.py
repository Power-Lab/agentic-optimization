"""Execution transports for the remote backend.

The remote adapter's orchestration (marshal config -> run in a working tree ->
pull results back) is identical whether the working tree is on a colo node or
on this machine. Only *how* a command runs and *how* files move differs. That
mechanism lives here behind a small interface so the orchestration can be tested
locally (``LocalTransport``) before pointing it at a real node (``SshTransport``).
"""

from __future__ import annotations

import subprocess
from abc import ABC, abstractmethod
from typing import List, Sequence


class Transport(ABC):
    """How the remote adapter executes commands and moves files."""

    @abstractmethod
    def run(self, command: str, capture: bool = True) -> subprocess.CompletedProcess:
        """Run a shell command in the (possibly remote) execution environment."""

    @abstractmethod
    def push(self, local_path: str, remote_path: str) -> subprocess.CompletedProcess:
        """Copy a local file/dir to the execution environment."""

    @abstractmethod
    def pull(self, remote_path: str, local_path: str) -> subprocess.CompletedProcess:
        """Copy a file/dir from the execution environment back to local."""

    @property
    def description(self) -> str:
        return self.__class__.__name__


class SshTransport(Transport):
    """Real remote execution: ssh for commands, rsync over ssh for files."""

    def __init__(self, host: str, opts: Sequence[str] = ()):
        self.host = host
        self.opts: List[str] = list(opts)

    @property
    def _rsync_e(self) -> List[str]:
        return ["-e", " ".join(["ssh", *self.opts])] if self.opts else []

    def run(self, command: str, capture: bool = True) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["ssh", *self.opts, self.host, command], capture_output=capture, text=True
        )

    def push(self, local_path: str, remote_path: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["rsync", "-az", *self._rsync_e, local_path, f"{self.host}:{remote_path}"],
            capture_output=True, text=True,
        )

    def pull(self, remote_path: str, local_path: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["rsync", "-az", *self._rsync_e, f"{self.host}:{remote_path}", local_path],
            capture_output=True, text=True,
        )

    @property
    def description(self) -> str:
        return f"ssh:{self.host}"


class LocalTransport(Transport):
    """Loopback transport: same orchestration, no network. For local testing of
    the remote backend — runs commands in a local login shell and rsyncs between
    local paths (the 'remote' working tree is just a local directory)."""

    def run(self, command: str, capture: bool = True) -> subprocess.CompletedProcess:
        return subprocess.run(["bash", "-lc", command], capture_output=capture, text=True)

    def push(self, local_path: str, remote_path: str) -> subprocess.CompletedProcess:
        return subprocess.run(["rsync", "-az", local_path, remote_path],
                              capture_output=True, text=True)

    def pull(self, remote_path: str, local_path: str) -> subprocess.CompletedProcess:
        return subprocess.run(["rsync", "-az", remote_path, local_path],
                              capture_output=True, text=True)

    @property
    def description(self) -> str:
        return "local"
