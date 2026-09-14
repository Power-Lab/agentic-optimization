"""Resource adequacy model adapter registration."""

from framework.registry import register_adapter
from adapters.resource_adequacy.resource_adequacy_adapter import ResourceAdequacyAdapter

register_adapter("resource_adequacy", ResourceAdequacyAdapter)

__all__ = ["ResourceAdequacyAdapter"]
