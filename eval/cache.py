"""Solve cache — identical configs are solved once (protocol §8, cost controls).

The cache is keyed on ``(adapter.name, config_hash(config))``. A completed run
directory is copied into ``<cache_root>/<adapter>/<hash>/`` together with a
``run_record.json`` carrying the :class:`Execution`. On a hit, the cached files
(``solver.log``, ``outputs/``, ``config.json``, ...) are copied into the new run
directory, a ``cache_hit.json`` marker is written, and the cached ``Execution``
is returned — so ``run_and_record`` produces a normal run record whose only
difference is that no solver ran.

Only *settled* results are cached: OPTIMAL, INFEASIBLE, TIME_LIMIT, and ERROR
with a deterministic origin (preflight/solver). Runtime errors (crashes, missing
binaries) are not cached because they may be transient.
"""

from __future__ import annotations

import inspect
import json
import shutil
import time
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Union

from framework.adapter import Adapter, ValidationResult
from framework.interventions import InterventionSpec
from framework.run_record import Execution, RunRecord, config_hash

CACHEABLE_STATUSES = {"OPTIMAL", "INFEASIBLE", "TIME_LIMIT"}
#: ERROR origins that are a deterministic property of the *config*: the adapter's
#: own validation ("preflight") and the solver's verdict ("solver"). "runtime"
#: and "environment" are excluded on purpose — a crash may be transient, and an
#: environment failure (a missing interpreter, an un-importable model package)
#: stops being true the moment the machine is fixed, while the cache key is the
#: config hash alone and would otherwise replay the failure forever.
CACHEABLE_ERROR_ORIGINS = {"preflight", "solver"}
RECORD_NAME = "run_record.json"
MARKER_NAME = "cache_hit.json"


def default_cacheable(execution: Execution) -> bool:
    status = execution.termination_status
    if status in CACHEABLE_STATUSES:
        return True
    if status == "ERROR" and execution.error_origin in CACHEABLE_ERROR_ORIGINS:
        return True
    return False


class SolveCache:
    """Directory-backed cache of completed runs keyed by (adapter, config hash)."""

    def __init__(self, root: Union[str, Path]):
        self.root = Path(root)
        self.hits = 0
        self.misses = 0
        self.stores = 0

    # ---- keys --------------------------------------------------------------

    @staticmethod
    def key(adapter_name: str, config: Dict[str, Any]) -> str:
        return config_hash(config)

    def entry_dir(self, adapter_name: str, config: Dict[str, Any]) -> Path:
        return self.root / adapter_name / self.key(adapter_name, config)

    def has(self, adapter_name: str, config: Dict[str, Any]) -> bool:
        return (self.entry_dir(adapter_name, config) / RECORD_NAME).exists()

    def lookup(self, adapter_name: str, config: Dict[str, Any]) -> Optional[Path]:
        d = self.entry_dir(adapter_name, config)
        return d if (d / RECORD_NAME).exists() else None

    # ---- store / restore ---------------------------------------------------

    def store(self, adapter_name: str, config: Dict[str, Any], run_dir: Union[str, Path],
              execution: Execution) -> Path:
        """Copy a completed run directory into the cache (atomic rename)."""
        run_dir = Path(run_dir)
        final = self.entry_dir(adapter_name, config)
        final.parent.mkdir(parents=True, exist_ok=True)
        tmp = final.parent / (final.name + ".tmp")
        if tmp.exists():
            shutil.rmtree(tmp)
        tmp.mkdir()
        for item in run_dir.iterdir():
            if item.name in (RECORD_NAME, MARKER_NAME):
                continue
            dest = tmp / item.name
            if item.is_dir():
                shutil.copytree(item, dest, symlinks=True)
            else:
                shutil.copy2(item, dest)
        record = RunRecord(config=dict(config), execution=execution)
        record.save(tmp / RECORD_NAME)
        (tmp / "cache_meta.json").write_text(json.dumps({
            "adapter": adapter_name,
            "config_hash": record.config_hash,
            "stored_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "source_run_dir": str(run_dir),
        }, indent=2))
        if final.exists():
            shutil.rmtree(final)
        tmp.rename(final)
        self.stores += 1
        return final

    def restore(self, entry: Union[str, Path], run_dir: Union[str, Path]) -> Execution:
        """Copy a cache entry's files into ``run_dir`` and return its Execution."""
        entry = Path(entry)
        run_dir = Path(run_dir)
        run_dir.mkdir(parents=True, exist_ok=True)
        for item in entry.iterdir():
            if item.name in (RECORD_NAME, "cache_meta.json"):
                continue
            dest = run_dir / item.name
            if item.is_dir():
                if dest.exists():
                    shutil.rmtree(dest)
                shutil.copytree(item, dest, symlinks=True)
            else:
                shutil.copy2(item, dest)
        record = RunRecord.load(entry / RECORD_NAME)
        (run_dir / MARKER_NAME).write_text(json.dumps({
            "cache_entry": str(entry),
            "config_hash": record.config_hash,
            "restored_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        }, indent=2))
        return record.execution

    def stats(self) -> Dict[str, int]:
        return {"hits": self.hits, "misses": self.misses, "stores": self.stores}


class CachedAdapter(Adapter):
    """Wrap any adapter so identical configs reuse a cached solve.

    Everything except ``run`` is delegated to the wrapped adapter (unknown
    attributes too, so adapter-specific helpers stay reachable).
    """

    def __init__(self, inner: Adapter, cache: SolveCache,
                 cacheable: Optional[Callable[[Execution], bool]] = None):
        self.inner = inner
        self.cache = cache
        self.name = inner.name
        self._cacheable = cacheable or default_cacheable
        self.run_calls = 0      # number of real (uncached) solves

    # delegation ------------------------------------------------------------

    def validate_config(self, config: Dict[str, Any]) -> ValidationResult:
        return self.inner.validate_config(config)

    def intervention_spec(self) -> InterventionSpec:
        return self.inner.intervention_spec()

    def locate_outputs(self, run_dir: Path) -> Dict[str, Path]:
        return self.inner.locate_outputs(Path(run_dir))

    def describe_config(self) -> str:
        return self.inner.describe_config()

    def __getattr__(self, item: str) -> Any:
        # Only reached for attributes not found normally.
        inner = self.__dict__.get("inner")
        if inner is None:
            raise AttributeError(item)
        return getattr(inner, item)

    # the cached run --------------------------------------------------------

    def run(self, config: Dict[str, Any], run_dir: Path, on_event: Any = None, **kwargs: Any) -> Execution:
        run_dir = Path(run_dir)
        entry = self.cache.lookup(self.name, config)
        if entry is not None:
            self.cache.hits += 1
            return self.cache.restore(entry, run_dir)
        self.cache.misses += 1
        self.run_calls += 1
        execution = self._call_inner(config, run_dir, on_event, kwargs)
        if self._cacheable(execution):
            self.cache.store(self.name, config, run_dir, execution)
        return execution

    def _call_inner(self, config, run_dir, on_event, kwargs) -> Execution:
        run = self.inner.run
        try:
            params = inspect.signature(run).parameters
        except (TypeError, ValueError):
            params = {}
        accepts_var_kw = any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values())
        call_kwargs: Dict[str, Any] = {}
        if on_event is not None and ("on_event" in params or accepts_var_kw):
            call_kwargs["on_event"] = on_event
        for k, v in kwargs.items():
            if k in params or accepts_var_kw:
                call_kwargs[k] = v
        return run(config, run_dir, **call_kwargs)
