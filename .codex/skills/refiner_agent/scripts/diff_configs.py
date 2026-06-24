"""
diff_configs.py
----------------
Prints a human-readable diff of changed keys between two YAML
scenario config files. Useful for auditing what the Refiner Agent changed.

CLI usage:
    python skill_library/refiner_agent/scripts/diff_configs.py \
        --before skill_library/refiner_agent/assets/failed_config.yaml \
        --after  skill_library/refiner_agent/assets/refined_config.yaml
"""

import argparse
from pathlib import Path

import yaml


def flatten(d: dict, prefix: str = "") -> dict:
    """Recursively flatten a nested dict to dot-notation keys."""
    flat = {}
    for k, v in d.items():
        full_key = f"{prefix}.{k}" if prefix else k
        if isinstance(v, dict):
            flat.update(flatten(v, full_key))
        elif isinstance(v, list):
            flat[full_key] = repr(v)
        else:
            flat[full_key] = v
    return flat


def diff_configs(before: dict, after: dict) -> list[dict]:
    """
    Compare two configs and return a list of changes.
    Each change is {key, before_value, after_value, change_type}.
    """
    flat_before = flatten(before)
    flat_after = flatten(after)

    all_keys = set(flat_before) | set(flat_after)
    changes = []

    for key in sorted(all_keys):
        b_val = flat_before.get(key, "<absent>")
        a_val = flat_after.get(key, "<absent>")
        if b_val == a_val:
            continue
        if b_val == "<absent>":
            change_type = "ADDED"
        elif a_val == "<absent>":
            change_type = "REMOVED"
        else:
            change_type = "CHANGED"
        changes.append({
            "key": key,
            "before": b_val,
            "after": a_val,
            "change_type": change_type,
        })

    return changes


def main() -> None:
    parser = argparse.ArgumentParser(description="Diff two scenario config YAML files.")
    parser.add_argument("--before", required=True, help="Path to original (failed) config.")
    parser.add_argument("--after", required=True, help="Path to refined config.")
    args = parser.parse_args()

    with open(args.before) as f:
        before = yaml.safe_load(f)
    with open(args.after) as f:
        after = yaml.safe_load(f)

    changes = diff_configs(before, after)

    if not changes:
        print("[diff_configs] No differences found.")
        return

    print(f"[diff_configs] {len(changes)} change(s) detected:\n")
    for c in changes:
        symbol = {"ADDED": "+", "REMOVED": "-", "CHANGED": "~"}[c["change_type"]]
        if c["change_type"] == "CHANGED":
            print(f"  {symbol} {c['key']}: {c['before']!r} → {c['after']!r}")
        elif c["change_type"] == "ADDED":
            print(f"  {symbol} {c['key']}: (new) {c['after']!r}")
        else:
            print(f"  {symbol} {c['key']}: {c['before']!r} (removed)")


if __name__ == "__main__":
    main()
