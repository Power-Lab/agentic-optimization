"""
parse_gurobi_log.py
--------------------
Extracts key diagnostic information from a Gurobi solver log file.
Produces a structured dict matching the log_analysis_report schema.

CLI usage:
    python skill_library/solver_log_analyzer/scripts/parse_gurobi_log.py \
        --log runs/run_001/solver.log

Prints a JSON summary to stdout.
"""

import argparse
import json
import re
from pathlib import Path


# --- Termination status patterns ---
TERMINATION_PATTERNS = {
    "OPTIMAL": [r"Optimal solution found", r"optimal solution"],
    "INFEASIBLE": [r"Model is infeasible\b"],
    "INFEASIBLE_OR_UNBOUNDED": [r"Model is infeasible or unbounded"],
    "TIME_LIMIT": [r"Time limit reached", r"time limit"],
    "NUMERICAL_ERROR": [r"Numeric focus required", r"numerical issues"],
}

# --- Numerical instability patterns ---
NUMERICAL_PATTERNS = {
    "LARGE_COEFFICIENTS": r"Numeric values are large",
    "COEFFICIENT_RANGE_WARNING": r"max constraint coefficient",
    "PRIMAL_DUAL_DISAGREEMENT": r"Dual objective and primal objective disagree",
    "INCUMBENT_STAGNATION": r"Solutions are not improving",
    "NO_PRESOLVE_REDUCTION": r"Presolve removed 0 rows and 0 columns",
}

# --- MIP progress table row ---
MIP_ROW_RE = re.compile(
    r"^\s*(\d+)\s+\d+\s+[\d.]+\s+\d+\s+\d*\s+([\d.]+)\s+([\d.]+)\s+([\d.]+)%"
)

# --- Presolve ---
PRESOLVE_RE = re.compile(r"Presolve:\s+(\d+) rows removed,\s+(\d+) columns removed")

# --- IIS ---
IIS_RE = re.compile(r"IIS computed:\s*(.+)")

# --- Objective line ---
OBJ_RE = re.compile(r"Best objective\s+([\d.]+),\s+best bound\s+([\d.]+),\s+gap\s+([\d.]+)%")

# --- Solve time ---
TIME_RE = re.compile(r"Solve time:\s+([\d.]+)s")


def parse_log(log_path: Path) -> dict:
    """Parse a Gurobi log file and return a structured diagnostic dict."""
    if not log_path.exists():
        return {"error": f"LOG_FILE_NOT_FOUND: {log_path}"}

    lines = log_path.read_text(errors="replace").splitlines()

    result = {
        "termination_status": None,
        "objective_value": None,
        "best_bound": None,
        "final_mip_gap_pct": None,
        "solve_time_s": None,
        "iis_constraints": [],
        "likely_cause": None,
        "presolve_rows_removed": None,
        "presolve_cols_removed": None,
        "gap_trajectory": [],
        "convergence_rate": None,
        "numerical_patterns": [],
        "recommendations": [],
    }

    for line in lines:
        # Termination status
        if result["termination_status"] is None:
            for status, patterns in TERMINATION_PATTERNS.items():
                for pat in patterns:
                    if re.search(pat, line, re.IGNORECASE):
                        result["termination_status"] = status
                        break

        # Presolve
        m = PRESOLVE_RE.search(line)
        if m:
            result["presolve_rows_removed"] = int(m.group(1))
            result["presolve_cols_removed"] = int(m.group(2))

        # IIS constraints
        m = IIS_RE.search(line)
        if m:
            result["iis_constraints"] = [c.strip() for c in m.group(1).split(",")]

        # MIP progress row
        m = MIP_ROW_RE.match(line)
        if m:
            try:
                gap = float(m.group(4))
                result["gap_trajectory"].append(gap)
            except ValueError:
                pass

        # Objective summary
        m = OBJ_RE.search(line)
        if m:
            result["objective_value"] = float(m.group(1))
            result["best_bound"] = float(m.group(2))
            result["final_mip_gap_pct"] = float(m.group(3))

        # Solve time
        m = TIME_RE.search(line)
        if m:
            result["solve_time_s"] = float(m.group(1))

        # Numerical instability
        for pattern_name, pattern in NUMERICAL_PATTERNS.items():
            if re.search(pattern, line, re.IGNORECASE):
                if pattern_name not in result["numerical_patterns"]:
                    result["numerical_patterns"].append(pattern_name)

    # Classify likely cause from IIS
    iis = result["iis_constraints"]
    if "emission_cap[total]" in iis and "renewable_penetration_minimum" in iis:
        result["likely_cause"] = "EMISSION_CAP_TOO_TIGHT"
    elif sum(1 for c in iis if "power_balance" in c) > 5:
        result["likely_cause"] = "DEMAND_SUPPLY_MISMATCH"
    elif any("ramp" in c for c in iis):
        result["likely_cause"] = "COMMITMENT_RAMP_INFEASIBILITY"

    # Classify convergence rate from gap trajectory
    traj = result["gap_trajectory"]
    if len(traj) >= 2:
        if traj[0] > 0 and traj[-1] / traj[0] < 0.01:
            result["convergence_rate"] = "FAST"
        elif any(traj[i] > traj[i-1] for i in range(1, len(traj))):
            result["convergence_rate"] = "DIVERGING"
            result["recommendations"].append("DIVERGING gap detected — numerical instability. Recommend immediate termination.")
        elif len(traj) >= 5 and max(traj[-5:]) - min(traj[-5:]) < 0.0001:
            result["convergence_rate"] = "STALLED"
            result["recommendations"].append("Gap stalled. Recommend early stop and warm start on next iteration.")
        else:
            result["convergence_rate"] = "NORMAL"

    # Overall recommendations
    if result["termination_status"] == "INFEASIBLE":
        result["recommendations"].append(
            f"Infeasibility detected. Likely cause: {result['likely_cause']}. "
            "Route to Refiner Agent with INFEASIBILITY context."
        )
    if result["numerical_patterns"]:
        result["recommendations"].append(
            f"Numerical issues: {result['numerical_patterns']}. "
            "Recommend NumericFocus=3, ScaleFlag=2 on next run."
        )

    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="Parse a Gurobi solver log file.")
    parser.add_argument("--log", required=True, help="Path to solver.log.")
    args = parser.parse_args()

    result = parse_log(Path(args.log))
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
