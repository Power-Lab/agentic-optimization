"""Natural-language planner and safe execution gate for four Power Lab models.

The default is planning-only: route the request, build a conservative config,
validate it through the selected adapter, and explain intervention tiers.
Actual Julia/solver execution requires the explicit ``--run`` flag.
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any, Dict, Tuple

from framework.registry import get_adapter
from framework.runner import run_and_record

MODELS = ("garuda", "captive", "storage", "resource_adequacy")

DEFAULTS: Dict[str, Dict[str, Any]] = {
    "garuda": {
        "island": "maluku", "year": "2030", "scenario": "base",
        "clean": "reference", "CO235reduction": False,
        "BAUCO2emissions": 0.0, "CO2_limit": 5_820_000.0,
        "engine": "dispatch", "solver": "highs", "relax_uc": True,
    },
    "captive": {
        "island": "maluku", "year": "2030", "scenario": "captive",
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


def route(prompt: str, model_override: str | None = None) -> Tuple[str, str]:
    if model_override:
        return model_override, "selected by --model override"
    text = prompt.lower()
    if any(term in text for term in ("battery", "storage", "four-hour", "duration")):
        return "storage", "battery/storage language"
    if any(term in text for term in ("resource adequacy", "uced", "northeast china", "derate")):
        return "resource_adequacy", "resource-adequacy/UCED language"
    if any(term in text for term in ("captive", "industrial park", "industrial power")):
        return "captive", "captive/industrial-park language"
    if any(term in text for term in ("garuda", "village", "maluku", "timor", "indonesia")):
        return "garuda", "Garuda/village geography language"
    raise ValueError("Could not safely identify a model; add --model " + "|".join(MODELS))


def _number(pattern: str, text: str, default: Any) -> Any:
    match = re.search(pattern, text)
    if not match:
        return default
    value = float(match.group(1))
    return int(value) if value.is_integer() else value


def _numericize(text: str) -> str:
    """Normalize common spoken small numbers used in scenario prompts."""
    words = {
        "one": "1", "two": "2", "three": "3", "four": "4", "five": "5",
        "six": "6", "seven": "7", "eight": "8", "nine": "9", "ten": "10",
    }
    for word, number in words.items():
        text = re.sub(rf"\b{word}\b", number, text)
    return text


def build_config(model: str, prompt: str) -> Dict[str, Any]:
    text = prompt.lower()
    config = dict(DEFAULTS[model])
    if model == "storage":
        text = _numericize(text)
        config["storage_gw"] = _number(r"(\d+(?:\.\d+)?)\s*gw", text, config["storage_gw"])
        config["duration_hours"] = _number(
            r"(\d+(?:\.\d+)?)\s*(?:-|\s)?hour", text, config["duration_hours"]
        )
        config["simulation_days"] = _number(
            r"(\d+(?:\.\d+)?)\s*(?:-|\s)?day", text, config["simulation_days"]
        )
        if "triple" in text:
            config["wind_scale"] = config["solar_scale"] = 3
        config["wind_scale"] = _number(
            r"wind(?:\s+capacity)?(?:\s+scale)?\s*(?:of|=|to)?\s*(\d+(?:\.\d+)?)",
            text, config["wind_scale"],
        )
        config["solar_scale"] = _number(
            r"solar(?:\s+capacity)?(?:\s+scale)?\s*(?:of|=|to)?\s*(\d+(?:\.\d+)?)",
            text, config["solar_scale"],
        )
        config["production_incentive"] = _number(
            r"(?:ptc|production incentive)(?:\s+of|\s*=|\s+to)?\s*\$?(\d+(?:\.\d+)?)",
            text, config["production_incentive"],
        )
        return config

    if model == "resource_adequacy":
        return config

    year = re.search(r"\b(20\d{2})\b", text)
    if year:
        config["year"] = year.group(1)
    explicit_island = False
    for island in ("maluku", "timor_demo", "timor", "papua", "sulawesi", "sumatera", "jawa_bali"):
        if island in text:
            config["island"] = island
            explicit_island = True
            break
    if "clean" in text:
        config["clean"] = "clean"
    if model == "captive":
        if "grid" in text and "captive" in text:
            config["scenario"] = "gridcaptive"
        elif "no coal" in text or "nocoal" in text:
            config["scenario"] = "nocoal"
        elif "high import" in text:
            config["scenario"] = "highimportprice"
        elif "reference" in text:
            config["scenario"] = "captive"
    else:
        if "grid" in text and "village" in text:
            config["scenario"] = "gridvillage"
        elif "village" in text:
            config["scenario"] = "village"
        elif "grid" in text:
            config["scenario"] = "grid"
        elif "no coal" in text or "nocoal" in text:
            config["scenario"] = "nocoal"
        # The bundled Maluku checkout has grid tables but no village/site
        # tables. timor_demo is the small, complete dataset intended for this
        # end-to-end demonstration. Never override a place the user named.
        if "village" in text and not explicit_island:
            config["island"] = "timor_demo"
    return config


def tier_map(adapter: Any, config: Dict[str, Any]) -> Dict[str, str]:
    spec = adapter.intervention_spec()
    result = {}
    for key in config:
        if key in spec.tier_a_keys:
            result[key] = "A"
        elif key in spec.tier_b_keys:
            result[key] = "B"
        else:
            result[key] = "C"
    return result


def plan(prompt: str, model_override: str | None = None) -> Dict[str, Any]:
    model, reason = route(prompt, model_override)
    adapter = get_adapter(model)
    config = build_config(model, prompt)
    validation = adapter.validate_config(config)
    warnings = []
    if model == "resource_adequacy":
        warnings.append("The upstream runner executes the complete 54-case factorial batch.")
    if model == "storage":
        warnings.append("The upstream runner executes all 90 periods.")
    return {
        "prompt": prompt,
        "selected_model": model,
        "routing_reason": reason,
        "config": config,
        "tiers": tier_map(adapter, config),
        "validation": {"ok": validation.ok, "errors": validation.errors},
        "warnings": warnings,
        "execution_authorized": False,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("prompt")
    parser.add_argument("--model", choices=MODELS)
    parser.add_argument("--run", action="store_true")
    parser.add_argument("--run-dir", type=Path)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    try:
        result = plan(args.prompt, args.model)
    except ValueError as exc:
        parser.error(str(exc))

    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print(f"SELECTED MODEL: {result['selected_model']}")
        print(f"WHY: {result['routing_reason']}")
        print("CONFIG:")
        print(json.dumps(result["config"], indent=2))
        print("GUARDRAIL TIERS:")
        print(json.dumps(result["tiers"], indent=2))
        print("VALIDATION:", "PASS" if result["validation"]["ok"] else "FAIL")
        for warning in result["warnings"]:
            print("WARNING:", warning)
    if not result["validation"]["ok"]:
        for error in result["validation"]["errors"]:
            print("ERROR:", error)
        return 2
    if not args.run:
        print("PLAN ONLY: add --run to launch the selected model")
        return 0

    adapter = get_adapter(result["selected_model"])
    run_dir = args.run_dir or Path("runs") / f"agent_{result['selected_model']}"
    record = run_and_record(adapter, result["config"], run_dir)
    print("RUN STATUS:", record.execution.termination_status)
    print("WALL SECONDS:", record.execution.wall_seconds)
    print("RUN RECORD:", run_dir / "run_record.json")
    return 0 if record.execution.returncode == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
