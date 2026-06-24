"""
check_power_balance.py
-----------------------
Reads a dispatch CSV and flags any timesteps where supply does not
equal demand within the specified tolerance.

CLI usage:
    python skill_library/output_analysis_agent/scripts/check_power_balance.py \
        --dispatch runs/run_001/dispatch.csv \
        --tolerance 0.5

Prints a report to stdout and exits 0 if OK, 1 if violations found.
"""

import argparse
import csv
import sys
from pathlib import Path


def check_balance(dispatch_path: Path, tolerance_MW: float = 0.5) -> dict:
    """
    Load dispatch CSV and check power balance at each timestep.

    Returns a dict with:
        - status: OK | ANOMALY
        - violations: list of {timestep, demand_MW, supply_MW, imbalance_MW}
        - max_imbalance_MW: float
        - total_timesteps: int
    """
    violations = []
    max_imbalance = 0.0
    total = 0

    with open(dispatch_path) as f:
        reader = csv.DictReader(f)
        fieldnames = reader.fieldnames or []
        gen_cols = [c for c in fieldnames
                    if c not in ("timestep", "demand_MW", "load_shedding_MW")]

        for row in reader:
            total += 1
            t = int(row["timestep"])
            demand = float(row["demand_MW"])
            shedding = float(row.get("load_shedding_MW", 0.0))
            supply = sum(float(row[g]) for g in gen_cols if row.get(g)) + shedding
            imbalance = abs(supply - demand)
            if imbalance > max_imbalance:
                max_imbalance = imbalance
            if imbalance > tolerance_MW:
                violations.append({
                    "timestep": t,
                    "demand_MW": round(demand, 3),
                    "supply_MW": round(supply, 3),
                    "imbalance_MW": round(imbalance, 3),
                })

    return {
        "status": "ANOMALY" if violations else "OK",
        "violations": violations,
        "max_imbalance_MW": round(max_imbalance, 3),
        "total_timesteps": total,
        "tolerance_MW": tolerance_MW,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Check power balance in dispatch CSV.")
    parser.add_argument("--dispatch", required=True, help="Path to dispatch.csv.")
    parser.add_argument("--tolerance", type=float, default=0.5,
                        help="Max allowed imbalance in MW (default: 0.5).")
    args = parser.parse_args()

    path = Path(args.dispatch)
    if not path.exists():
        print(f"[check_power_balance] ERROR: File not found: {path}")
        sys.exit(1)

    result = check_balance(path, tolerance_MW=args.tolerance)

    print(f"[check_power_balance] Status: {result['status']}")
    print(f"  Timesteps checked : {result['total_timesteps']}")
    print(f"  Max imbalance     : {result['max_imbalance_MW']:.3f} MW")
    print(f"  Tolerance         : {result['tolerance_MW']:.1f} MW")
    print(f"  Violations        : {len(result['violations'])}")
    for v in result["violations"]:
        print(f"    t={v['timestep']:>2}: demand={v['demand_MW']:.2f}, "
              f"supply={v['supply_MW']:.2f}, imbalance={v['imbalance_MW']:.3f} MW")

    sys.exit(0 if result["status"] == "OK" else 1)


if __name__ == "__main__":
    main()
