"""Config schema for the pypsa_toy adapter — pure Python, no pypsa import.

Everything the framework needs to *reason about* a config (which keys exist,
their tier, defaults, legal ranges, semantics) lives here so that the adapter,
the fixtures' tests and the scenario-builder skill can load it in any
interpreter. Only ``network.py``/``runner.py`` import pypsa.

Tier declaration (the model's contribution to the guardrail):

- **C (policy, human sign-off):** ``co2_cap_t``, ``re_share_min``,
  ``allow_load_shedding`` — relaxing any of these changes what the study claims.
- **B (sanctioned parameters, auto + flag):** ``demand_scale``, ``gas_price``,
  ``coal_price``, ``wind_capex``, ``solar_capex``, ``battery_capex``,
  ``line_expansion_allowed``, ``snapshots_days``.
- **A (numerics, auto):** ``solver``, ``mip_gap``, ``time_limit``, ``threads``.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

ADAPTER_NAME = "pypsa_toy"

# Solver enum. The toy is a pure LP so HiGHS is the only supported backend.
SOLVER_ENUM = ["highs"]
SNAPSHOT_DAYS_ENUM = list(range(1, 15))  # 1..14 inclusive
MAX_DAYS = 14


@dataclass(frozen=True)
class KeySpec:
    key: str
    tier: str                      # "A" | "B" | "C"
    type: str                      # "float" | "int" | "bool" | "str" | "float|null"
    default: Any
    description: str
    minimum: Optional[float] = None   # inclusive
    maximum: Optional[float] = None   # inclusive
    exclusive_minimum: bool = False   # if True the bound at `minimum` is exclusive
    enum: Optional[List[Any]] = None


KEY_SPECS: List[KeySpec] = [
    # ---- Tier C: policy constraints -----------------------------------------
    KeySpec(
        "co2_cap_t", "C", "float|null", None,
        "Cap on total CO2 emissions over the modelled horizon, in tonnes "
        "(pypsa GlobalConstraint on carrier co2_emissions, sense <=). null "
        "means no cap. POLICY LEVER: relaxing it changes the study's claim. "
        "Physical floor: the existing coal unit is must-run at 20 % of 300 MW, "
        "so emissions cannot go below ~1289 t/day (see describe_config); any cap "
        "below that floor is infeasible for EVERY choice of the other keys. The cap is "
        "ABSOLUTE over the horizon, not per day, so it is scaled by the keys that size "
        "the problem: shrinking snapshots_days or demand_scale lowers the emissions the "
        "cap has to bind, which is why those two moves are Tier C while a cap is set "
        "(see TRANSITION_RULES in pypsa_adapter.py).",
        minimum=0.0,
    ),
    KeySpec(
        "re_share_min", "C", "float|null", None,
        "Minimum share of total generation (excluding load shedding) that must "
        "come from wind + solar over the horizon, 0..1. null means no floor. "
        "POLICY LEVER. Wind (<=600 MW) and solar (<=800 MW) technical potential "
        "sums to ~56 % of default demand over the 14-day horizon (~50 % over 1 "
        "day), and the must-run coal unit puts an absolute ceiling near 0.92 at "
        "any demand level, so a floor at or above 0.9 is unattainable outright.",
        minimum=0.0, maximum=1.0,
    ),
    KeySpec(
        "allow_load_shedding", "C", "bool", False,
        "If true, a load-shedding generator (VOLL = 10,000 $/MWh, carrier "
        "load_shedding, zero emissions) is added at every bus so demand can be "
        "left unserved at a price. POLICY LEVER: enabling it converts an "
        "infeasible 'must serve all demand' study into a different study.",
        enum=[True, False],
    ),
    # ---- Tier B: sanctioned parameters --------------------------------------
    KeySpec(
        "demand_scale", "B", "float", 1.0,
        "Multiplier on every bus's demand profile (base peak ~1000 MW system-"
        "wide: north 500, east 300, south 200). Legal range 0.25..10. LOWERING it "
        "while co2_cap_t is set is Tier C: the absolute cap is written against the "
        "study's demand, so scaling demand down loosens the cap instead of meeting it.",
        minimum=0.25, maximum=10.0,
    ),
    KeySpec(
        "gas_price", "B", "float", 25.0,
        "Gas fuel price in $/MWh_thermal (gas CCGT efficiency 0.55, VOM 2 $/MWh). "
        "Default 25. 0 is legal but produces the planted 'free gas' anomaly "
        "(gas takes ~all dispatch).",
        minimum=0.0,
    ),
    KeySpec(
        "coal_price", "B", "float", 10.0,
        "Coal fuel price in $/MWh_thermal (existing coal eff. 0.38, new coal "
        "0.42, VOM 3 $/MWh). Default 10.",
        minimum=0.0,
    ),
    KeySpec(
        "wind_capex", "B", "float", 110_000.0,
        "Annualised wind capital cost in $/MW/yr (pro-rated to the horizon). "
        "Default 110,000, which is ~27 $/MWh at the east bus's 46.5 % mean "
        "capacity factor.",
        minimum=0.0,
    ),
    KeySpec(
        "solar_capex", "B", "float", 80_000.0,
        "Annualised solar capital cost in $/MW/yr (pro-rated). Default 80,000, "
        "which is ~58 $/MWh at the south bus's 15.8 % mean capacity factor.",
        minimum=0.0,
    ),
    KeySpec(
        "battery_capex", "B", "float", 60_000.0,
        "Annualised 4-hour battery capital cost in $/MW/yr (pro-rated). "
        "Default 60,000. Round-trip efficiency 0.9.",
        minimum=0.0,
    ),
    KeySpec(
        "line_expansion_allowed", "B", "bool", True,
        "If true the three AC lines (north-east 400 MW, north-south 400 MW, "
        "east-south 200 MW) are extendable at 20,000 $/MW/yr (pro-rated) up to "
        "20 GW. If false they are fixed — the east/south buses can then only be "
        "served by local wind/solar/battery plus 600/600 MW of import.",
        enum=[True, False],
    ),
    KeySpec(
        "snapshots_days", "B", "int", 14,
        "Number of days of hourly snapshots, 1..14 (14 = 336 h). Day k of a "
        "shorter horizon is identical to day k of the full 14-day series, so "
        "1-day runs are a strict subset (useful for fast tests). SHORTENING it while "
        "co2_cap_t is set is Tier C: the cap is absolute over the horizon, so a shorter "
        "horizon makes the same cap non-binding and turns the study into a different, "
        "shorter one.",
        minimum=1, maximum=MAX_DAYS, enum=SNAPSHOT_DAYS_ENUM,
    ),
    # ---- Tier A: numerics ---------------------------------------------------
    KeySpec(
        "solver", "A", "str", "highs",
        "LP solver. Only 'highs' is supported (licence-free, bundled via highspy).",
        enum=SOLVER_ENUM,
    ),
    KeySpec(
        "mip_gap", "A", "float", 0.01,
        "Relative MIP gap passed to HiGHS (mip_rel_gap). The toy is a pure LP so "
        "this is inert, but it is honoured for API completeness.",
        minimum=0.0,
    ),
    KeySpec(
        "time_limit", "A", "float", 600.0,
        "Solver wall-clock limit in seconds (HiGHS time_limit), > 0. Default "
        "600. A 14-day solve takes ~1-3 s on a laptop; a limit of 0.01 s "
        "reliably yields TIME_LIMIT.",
        minimum=0.0, exclusive_minimum=True,
    ),
    KeySpec(
        "threads", "A", "int", 1,
        "HiGHS thread count, >= 1. Default 1 (deterministic).",
        minimum=1,
    ),
]

SPEC_BY_KEY: Dict[str, KeySpec] = {s.key: s for s in KEY_SPECS}
KNOWN_KEYS: List[str] = [s.key for s in KEY_SPECS]
DEFAULTS: Dict[str, Any] = {s.key: s.default for s in KEY_SPECS}

TIER_A_KEYS = {s.key for s in KEY_SPECS if s.tier == "A"}
TIER_B_KEYS = {s.key for s in KEY_SPECS if s.tier == "B"}
TIER_C_KEYS = {s.key for s in KEY_SPECS if s.tier == "C"}

ALLOWED_VALUES: Dict[str, List[Any]] = {
    s.key: list(s.enum) for s in KEY_SPECS if s.enum is not None
}


def apply_defaults(config: Dict[str, Any]) -> Dict[str, Any]:
    """Return a full config: defaults overlaid with the user's keys (unknown
    keys are kept so validation can report them)."""
    full = dict(DEFAULTS)
    full.update(config or {})
    return full


def _is_number(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _check_type(spec: KeySpec, value: Any) -> Optional[str]:
    t = spec.type
    if t == "float|null":
        if value is None:
            return None
        if not _is_number(value):
            return f"{spec.key!r} must be a number or null, got {value!r}"
        if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
            return f"{spec.key!r} must be finite, got {value!r}"
        return None
    if t == "float":
        if not _is_number(value):
            return f"{spec.key!r} must be a number, got {value!r}"
        if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
            return f"{spec.key!r} must be finite, got {value!r}"
        return None
    if t == "int":
        if not (isinstance(value, int) and not isinstance(value, bool)):
            # accept integral floats (JSON often yields 7.0)
            if isinstance(value, float) and value.is_integer():
                return None
            return f"{spec.key!r} must be an integer, got {value!r}"
        return None
    if t == "bool":
        if not isinstance(value, bool):
            return f"{spec.key!r} must be true/false, got {value!r}"
        return None
    if t == "str":
        if not isinstance(value, str):
            return f"{spec.key!r} must be a string, got {value!r}"
        return None
    return None  # pragma: no cover


def validate(config: Dict[str, Any]) -> List[str]:
    """Filesystem-free schema validation. Returns a list of error strings
    (empty when the config is legal). Unknown keys are errors — the model has
    a closed schema and a misspelt key would otherwise be silently ignored."""
    errors: List[str] = []
    if not isinstance(config, dict):
        return [f"config must be a JSON object, got {type(config).__name__}"]

    unknown = sorted(k for k in config if k not in SPEC_BY_KEY)
    for k in unknown:
        hint = _closest_key(k)
        msg = f"Unknown config key {k!r}"
        if hint:
            msg += f" (did you mean {hint!r}?)"
        errors.append(msg + f"; legal keys: {', '.join(KNOWN_KEYS)}")

    for key, value in config.items():
        spec = SPEC_BY_KEY.get(key)
        if spec is None:
            continue
        type_err = _check_type(spec, value)
        if type_err:
            errors.append(type_err)
            continue
        if value is None:
            continue
        if spec.enum is not None and spec.type != "int":
            # bools/strings: exact membership (ints are range-checked below)
            if value not in spec.enum:
                errors.append(
                    f"{key!r} = {value!r} is not in the legal set {spec.enum!r}"
                )
                continue
        if _is_number(value):
            if spec.minimum is not None:
                if spec.exclusive_minimum and value <= spec.minimum:
                    errors.append(f"{key!r} must be > {spec.minimum}, got {value!r}")
                    continue
                if not spec.exclusive_minimum and value < spec.minimum:
                    errors.append(f"{key!r} must be >= {spec.minimum}, got {value!r}")
                    continue
            if spec.maximum is not None and value > spec.maximum:
                errors.append(f"{key!r} must be <= {spec.maximum}, got {value!r}")
                continue
    return errors


def _closest_key(bad: str) -> Optional[str]:
    """Tiny typo helper (no external deps): pick the known key with the
    smallest edit distance if it is close enough."""
    best, best_d = None, 10 ** 9
    for k in KNOWN_KEYS:
        d = _levenshtein(bad.lower(), k.lower())
        if d < best_d:
            best, best_d = k, d
    return best if best is not None and best_d <= max(2, len(bad) // 3) else None


def _levenshtein(a: str, b: str) -> int:
    if a == b:
        return 0
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def tier_table() -> List[Tuple[str, str, str, Any]]:
    """(key, tier, type, default) rows for docs/describe_config."""
    return [(s.key, s.tier, s.type, s.default) for s in KEY_SPECS]
