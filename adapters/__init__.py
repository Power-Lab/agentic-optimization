"""Adapters package. Importing it registers every bundled adapter so the
framework's registry can resolve them by name.

Each model is a case study, not the framework: ``garuda`` is the reference
adapter, ``pathways`` is the second real lab model, and ``pypsa_toy`` is the
small fully reproducible one. Every import is guarded so a missing optional
dependency (or a checkout without one of the model submodules) degrades to
"that adapter is unavailable" instead of taking the whole registry down.

``framework.registry.get_adapter(name)`` resolves one by name;
``get_adapter()`` with no name needs ``AGENTIC_ADAPTER`` set once more than one
adapter is registered.
"""

from __future__ import annotations

#: Adapter packages that self-register on import, in resolution order.
BUNDLED = (
    "garuda",
    "captive",
    "storage",
    "resource_adequacy",
    "pathways",
    "pypsa_toy",
)

#: Names of the adapters that failed to import, mapped to the exception text.
#: Useful when ``get_adapter("x")`` raises and you want to know why.
UNAVAILABLE: dict[str, str] = {}


def _load_bundled() -> None:
    import importlib

    for _name in BUNDLED:
        try:
            importlib.import_module(f"adapters.{_name}")
        except ImportError as exc:  # pragma: no cover - optional dependency
            UNAVAILABLE[_name] = f"{type(exc).__name__}: {exc}"


_load_bundled()

__all__ = ["BUNDLED", "UNAVAILABLE"]
