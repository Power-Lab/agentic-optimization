"""Adapter registry — how skills find the active model without naming it.

Skills are framework-level and must not hard-code a specific model. They call
:func:`get_adapter`, which resolves the active adapter from (in order) an
explicit name, the ``AGENTIC_ADAPTER`` env var, or — if exactly one adapter is
registered — that one. Models register themselves on import (see
``adapters/<name>/__init__.py``); each model is one such case study, not the
framework.
"""

from __future__ import annotations

import importlib
import os
from typing import Callable, Dict, List, Optional

from framework.adapter import Adapter

_REGISTRY: Dict[str, Callable[..., Adapter]] = {}


def register_adapter(name: str, factory: Callable[..., Adapter]) -> None:
    """Register an adapter factory (usually the adapter class) under ``name``."""
    _REGISTRY[name] = factory


def _ensure_loaded() -> None:
    # Importing the adapters package triggers each adapter's self-registration.
    if not _REGISTRY:
        try:
            importlib.import_module("adapters")
        except ModuleNotFoundError:
            pass


def available_adapters() -> List[str]:
    _ensure_loaded()
    return sorted(_REGISTRY)


def get_adapter(name: Optional[str] = None, **kwargs) -> Adapter:
    """Return an instance of the active adapter.

    Resolution: explicit ``name`` → ``AGENTIC_ADAPTER`` env → sole registered
    adapter. Raises if ambiguous or unknown. Extra kwargs go to the factory.
    """
    _ensure_loaded()
    if name is None:
        name = os.environ.get("AGENTIC_ADAPTER")
    if name is None:
        if len(_REGISTRY) == 1:
            name = next(iter(_REGISTRY))
        elif not _REGISTRY:
            raise LookupError(
                "No adapter is registered. Import the package that registers "
                "one (the bundled ones self-register from `import adapters`)."
            )
        else:
            raise LookupError(
                "No adapter specified and more than one is registered. "
                f"Set AGENTIC_ADAPTER or pass name=. Available: {sorted(_REGISTRY)}"
            )
    if name not in _REGISTRY:
        raise LookupError(f"Unknown adapter {name!r}. Available: {available_adapters()}")
    return _REGISTRY[name](**kwargs)
