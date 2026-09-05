"""Case study 2: the China renewable-energy Pathways model.

A second *real* lab model behind the same adapter contract as the reference
adapter — different language conventions, different failure modes, no config
file at all — which is what makes the framework's model-agnosticism falsifiable
rather than asserted.

Importing this package registers the adapter as ``"pathways"``.
"""

from framework.registry import register_adapter

from adapters.pathways import shim
from adapters.pathways.pathways_adapter import (
    DEFAULT_MODEL_ROOT,
    DRIVER_PATH,
    MODEL_REPO_URL,
    SMOKE_CONFIG,
    PathwaysAdapter,
    default_data_root,
    default_python,
    describe_schema,
)
from adapters.pathways.shim import (
    ALLOWED_VALUES,
    DEFAULTS,
    TIER_A_KEYS,
    TIER_B_KEYS,
    TIER_C_KEYS,
    build_scen_params,
    build_workspace,
    parse_driver_output,
    parse_gurobi_status,
    validate_schema,
)

# Self-register this case study so framework.get_adapter("pathways") resolves it.
register_adapter("pathways", PathwaysAdapter)

__all__ = [
    "PathwaysAdapter",
    "shim",
    "DEFAULT_MODEL_ROOT",
    "DRIVER_PATH",
    "MODEL_REPO_URL",
    "SMOKE_CONFIG",
    "ALLOWED_VALUES",
    "DEFAULTS",
    "TIER_A_KEYS",
    "TIER_B_KEYS",
    "TIER_C_KEYS",
    "build_scen_params",
    "build_workspace",
    "default_data_root",
    "default_python",
    "describe_schema",
    "parse_driver_output",
    "parse_gurobi_status",
    "validate_schema",
]


def __getattr__(name):
    """``RemotePathwaysAdapter`` on demand: it reaches into the reference
    adapter's transport layer, which may not be present."""
    if name == "RemotePathwaysAdapter":
        from adapters.pathways.remote_adapter import RemotePathwaysAdapter

        return RemotePathwaysAdapter
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
