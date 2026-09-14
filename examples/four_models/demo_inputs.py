#!/usr/bin/env python3
"""Interactive four-model input demo; execution is opt-in with --run."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from framework.registry import get_adapter
from framework.runner import run_and_record


PRESETS = {
    "garuda": {
        "island": "maluku", "year": "2030", "scenario": "base",
        "clean": "reference", "CO235reduction": False,
        "BAUCO2emissions": 0.0, "CO2_limit": 5_820_000.0,
        "engine": "dispatch", "solver": "highs", "relax_uc": True,
    },
    "captive": {
        "island": "maluku", "year": "2030", "scenario": "base",
        "clean": "reference", "CO235reduction": False,
        "BAUCO2emissions": 0.0, "CO2_limit": 5_820_000.0,
    },
    "storage": {
        "storage_gw": 20, "duration_hours": 4, "wind_scale": 3,
        "solar_scale": 3, "simulation_days": 4,
        "production_incentive": 10,
    },
    "resource_adequacy": {"study": "2030_full_factorial"},
}


def parse_value(raw: str) -> Any:
    """Parse JSON scalars while leaving ordinary words as strings."""
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return raw


def apply_overrides(config: dict[str, Any], overrides: list[str]) -> dict[str, Any]:
    updated = dict(config)
    for item in overrides:
        if "=" not in item:
            raise ValueError(f"Expected KEY=VALUE, got {item!r}")
        key, raw = item.split("=", 1)
        if key not in updated:
            raise ValueError(f"Unknown input {key!r}; legal inputs: {', '.join(updated)}")
        updated[key] = parse_value(raw)
    return updated


def tier_for(adapter: Any, key: str) -> str:
    spec = adapter.intervention_spec()
    if key in spec.tier_a_keys:
        return "A — automatic numeric/search setting"
    if key in spec.tier_b_keys:
        return "B — sanctioned scenario input"
    if key in spec.tier_c_keys:
        return "C — policy input; human approval required"
    return "C — unknown inputs default to human review"


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Change and validate inputs for four Power Lab models."
    )
    parser.add_argument("--model", choices=PRESETS, default="garuda")
    parser.add_argument("--set", action="append", default=[], metavar="KEY=VALUE")
    parser.add_argument("--show-all", action="store_true")
    parser.add_argument("--run", action="store_true",
                        help="Launch the real solver (dry-run is the default)")
    parser.add_argument("--run-dir", type=Path)
    args = parser.parse_args()

    if args.show_all:
        print(json.dumps(PRESETS, indent=2))
        return 0

    adapter = get_adapter(args.model)
    try:
        config = apply_overrides(PRESETS[args.model], args.set)
    except ValueError as exc:
        parser.error(str(exc))

    print(f"MODEL: {args.model}")
    print("INPUTS:")
    print(json.dumps(config, indent=2))
    if args.set:
        print("CHANGED INPUTS:")
        for item in args.set:
            key = item.split("=", 1)[0]
            print(f"- {key}: Tier {tier_for(adapter, key)}")

    validation = adapter.validate_config(config)
    print(f"VALIDATION: {'PASS' if validation.ok else 'FAIL'}")
    for error in validation.errors:
        print(f"- {error}")
    if not validation.ok:
        return 2

    if not args.run:
        print("MODE: DRY RUN — inputs are valid; add --run to launch the solver")
        return 0

    run_dir = args.run_dir or Path("runs") / f"demo_{args.model}"
    record = run_and_record(adapter, config, run_dir)
    print(f"STATUS: {record.execution.termination_status}")
    print(f"SECONDS: {record.execution.wall_seconds}")
    print(f"RUN RECORD: {run_dir / 'run_record.json'}")
    return 0 if record.execution.returncode == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
