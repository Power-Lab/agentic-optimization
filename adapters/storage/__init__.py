"""Energy storage model adapter registration."""

from framework.registry import register_adapter
from adapters.storage.storage_adapter import EnergyStorageAdapter

register_adapter("storage", EnergyStorageAdapter)

__all__ = ["EnergyStorageAdapter"]
