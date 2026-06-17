from framework.registry import register_adapter
from adapters.village.village_adapter import VillageAdapter
from adapters.village.remote_adapter import RemoteVillageAdapter
from adapters.village.transport import LocalTransport, SshTransport, Transport

# Self-register this case study so framework.get_adapter("village") resolves it.
register_adapter("village", VillageAdapter)

__all__ = [
    "VillageAdapter",
    "RemoteVillageAdapter",
    "Transport",
    "SshTransport",
    "LocalTransport",
]
