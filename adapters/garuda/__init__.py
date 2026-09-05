from framework.registry import register_adapter
from adapters.garuda.garuda_adapter import (
    GarudaAdapter,
    REGRESSION_CONFIG,
    REGRESSION_HEADLINES,
    REGRESSION_TOLERANCE,
    check_regression_headlines,
    describe_schema,
    read_headline,
    sanitise_run_tag,
    stream_local,
)
from adapters.garuda.remote_adapter import RemoteGarudaAdapter
from adapters.garuda.transport import LocalTransport, SshTransport, Transport

# Self-register this case study so framework.get_adapter("garuda") resolves it.
register_adapter("garuda", GarudaAdapter)

__all__ = [
    "GarudaAdapter",
    "RemoteGarudaAdapter",
    "Transport",
    "SshTransport",
    "LocalTransport",
    "REGRESSION_CONFIG",
    "REGRESSION_HEADLINES",
    "REGRESSION_TOLERANCE",
    "check_regression_headlines",
    "describe_schema",
    "read_headline",
    "sanitise_run_tag",
    "stream_local",
]
