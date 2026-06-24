"""
summarize_history.py
---------------------
Reads a run_history.jsonl file and prints a formatted summary of:
  - Objective value trend across iterations
  - Improvement percentages
  - Failure patterns encountered
  - Fix types applied

CLI usage:
    python skill_library/iteration_memory/scripts/summarize_history.py \
        --history skill_library/iteration_memory/assets/example_run_history.jsonl

    python skill_library/iteration_memory/scripts/summarize_history.py \
        --history run_history.jsonl \
        --session session_20240102_001
"""

import argparse
import json
from pathlib import Path


def load_history(path: Path, session_filter: str | None = None) -> list[dict]:
    """Load all entries from a JSONL history file, optionally filtered by session."""
    entries = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
                if session_filter and entry.get("session_id") != session_filter:
                    continue
                entries.append(entry)
            except json.JSONDecodeError as e:
                print(f"  [WARN] Skipping malformed line: {e}")
    return entries


def summarize(entries: list[dict]) -> None:
    """Print a formatted summary of iteration history."""
    if not entries:
        print("[summarize_history] No entries found.")
        return

    sessions = {}
    for e in entries:
        sid = e.get("session_id", "unknown")
        sessions.setdefault(sid, []).append(e)

    for session_id, runs in sessions.items():
        runs_sorted = sorted(runs, key=lambda r: r.get("iteration", 0))
        print(f"\n{'='*60}")
        print(f"Session: {session_id}  ({len(runs_sorted)} run(s))")
        print(f"{'='*60}")

        feasible_objs = []
        fix_counts: dict[str, int] = {}
        failures = 0

        for r in runs_sorted:
            it = r.get("iteration", "?")
            run_id = r.get("run_id", "?")
            status = r.get("solver_result", {}).get("status", "?")
            obj = r.get("solver_result", {}).get("objective_value_USD")
            ren = r.get("renewable_fraction")
            em = r.get("emissions_tCO2")
            fix = r.get("refiner_summary", {}).get("fix_applied")
            fix_type = r.get("refiner_summary", {}).get("fix_type")

            if obj is not None:
                feasible_objs.append(obj)

            if fix_type:
                fix_counts[fix_type] = fix_counts.get(fix_type, 0) + 1

            if status not in ("OPTIMAL", "TIME_LIMIT"):
                failures += 1

            obj_str = f"${obj:>12,.0f}" if obj is not None else "  INFEASIBLE  "
            ren_str = f"{ren:.1%}" if ren is not None else "  N/A  "
            em_str = f"{em:,.0f} tCO2" if em is not None else "  N/A  "

            print(f"  Iter {it:>2} | {run_id:<14} | {status:<12} | obj={obj_str} | "
                  f"ren={ren_str} | em={em_str}")
            if fix:
                print(f"           | fix: {fix}")

        print()
        if len(feasible_objs) >= 2:
            total_improvement = (feasible_objs[0] - feasible_objs[-1]) / feasible_objs[0] * 100
            print(f"  Objective improvement (first → last feasible): {total_improvement:.1f}%")
        print(f"  Feasible runs    : {len(feasible_objs)}")
        print(f"  Infeasible runs  : {failures}")
        if fix_counts:
            print(f"  Fix types applied: {fix_counts}")

        # Plateau check
        if len(feasible_objs) >= 3:
            last3 = feasible_objs[-3:]
            improvements = [abs(last3[i] - last3[i+1]) / max(last3[i], 1) for i in range(2)]
            if all(imp < 0.001 for imp in improvements):
                print("  [PLATEAU DETECTED]: Last 3 feasible runs improved by < 0.1%. "
                      "Consider TERMINATE_SUCCESS.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Summarize a run_history.jsonl file.")
    parser.add_argument("--history", required=True, help="Path to run_history.jsonl.")
    parser.add_argument("--session", default=None,
                        help="Filter to a specific session_id (default: all sessions).")
    args = parser.parse_args()

    path = Path(args.history)
    if not path.exists():
        print(f"[summarize_history] ERROR: File not found: {path}")
        return

    entries = load_history(path, session_filter=args.session)
    summarize(entries)


if __name__ == "__main__":
    main()
