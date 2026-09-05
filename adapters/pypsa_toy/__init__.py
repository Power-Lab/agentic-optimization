"""The ``pypsa_toy`` case study: a deterministic 3-bus PyPSA/HiGHS model.

Importing this package self-registers the adapter, so
``framework.get_adapter("pypsa_toy")`` resolves it. Only
:mod:`adapters.pypsa_toy.network` and :mod:`adapters.pypsa_toy.runner` import
pypsa/pandas; the adapter and the schema stay importable in a bare
interpreter, which is what lets the framework's tests and the scenario-builder
skill reason about this model without the scientific stack installed.
"""

from framework.registry import register_adapter

from adapters.pypsa_toy.pypsa_adapter import (
    PypsaToyAdapter,
    default_python,
    describe_schema,
    stream_local,
)
from adapters.pypsa_toy.schema import (
    ADAPTER_NAME,
    ALLOWED_VALUES,
    DEFAULTS,
    KEY_SPECS,
    KNOWN_KEYS,
    TIER_A_KEYS,
    TIER_B_KEYS,
    TIER_C_KEYS,
    apply_defaults,
    tier_table,
    validate,
)

register_adapter(ADAPTER_NAME, PypsaToyAdapter)

__all__ = [
    "ADAPTER_NAME",
    "ALLOWED_VALUES",
    "DEFAULTS",
    "KEY_SPECS",
    "KNOWN_KEYS",
    "PypsaToyAdapter",
    "TIER_A_KEYS",
    "TIER_B_KEYS",
    "TIER_C_KEYS",
    "apply_defaults",
    "default_python",
    "describe_schema",
    "stream_local",
    "tier_table",
    "validate",
]
