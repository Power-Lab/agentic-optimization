from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any


def append_history_record(history_path: Path, run_state: dict[str, Any]) -> dict[str, Any]:
    record = build_history_record(run_state)
    history_path.parent.mkdir(parents=True, exist_ok=True)
    with history_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, sort_keys=True) + "\n")
    return record


def build_history_record(run_state: dict[str, Any]) -> dict[str, Any]:
    executor = last_item(run_state.get("executor_results"))
    solve_summary = executor.get("solve_summary") if executor else None
    if not isinstance(solve_summary, dict):
        solve_summary = run_state.get("final_summary") if isinstance(run_state.get("final_summary"), dict) else {}

    validation = last_item(run_state.get("validation_results"))
    solver_log = last_item(run_state.get("solver_log_analysis_results"))
    output = last_item(run_state.get("output_analysis_results"))
    refiner = last_item(run_state.get("refinement_results"))
    iteration_state = run_state.get("iteration_state") if isinstance(run_state.get("iteration_state"), dict) else {}

    cause = refiner.get("cause") if refiner else None
    objective = solve_summary.get("objective_value_usd")
    solver_status = solve_summary.get("termination_status") or (executor.get("status") if executor else None)
    output_status = output.get("overall_status") if output else None
    log_status = solver_log.get("overall_status") if solver_log else None
    validation_status = validation.get("status") if validation else None
    is_feasible = run_state.get("status") == "COMPLETED" and solver_status in {"OPTIMAL", "FEASIBLE_POINT"}

    return {
        "run_id": run_state.get("run_id"),
        "scenario_id": run_state.get("scenario_id"),
        "scenario_config_hash": run_state.get("scenario_config_hash"),
        "scenario_config_path": run_state.get("scenario_config_path"),
        "run_dir": run_state.get("run_dir"),
        "timestamp": run_state.get("created_at"),
        "iteration": iteration_state.get("current_iteration"),
        "status": run_state.get("status"),
        "objective_value_usd": objective,
        "solver_status": solver_status,
        "validation_status": validation_status,
        "solver_log_status": log_status,
        "output_analysis_status": output_status,
        "refiner_recommendation": refiner.get("recommendation") if refiner else None,
        "refiner_fix_type": refiner.get("fix_type") if refiner else None,
        "cause": cause,
        "elapsed_wall_time_s": iteration_state.get("elapsed_wall_time_s"),
        "total_nse_mwh": solve_summary.get("total_nse_mwh"),
        "is_feasible": is_feasible,
        "failure_pattern_hash": failure_pattern_hash(run_state.get("scenario_config_hash"), cause)
        if cause and run_state.get("status") != "COMPLETED"
        else None,
    }


def summarize_history(history_path: Path) -> dict[str, Any]:
    records = read_history(history_path)
    feasible = [
        record for record in records
        if record.get("is_feasible") and isinstance(record.get("objective_value_usd"), (int, float))
    ]
    best = min(feasible, key=lambda record: record["objective_value_usd"]) if feasible else None
    trend = [
        {
            "run_id": record.get("run_id"),
            "iteration": record.get("iteration"),
            "objective_value_usd": record.get("objective_value_usd"),
            "status": record.get("solver_status"),
        }
        for record in feasible
    ]
    causes = [record.get("cause") for record in records if record.get("cause")]
    repeated_causes = {
        cause: count for cause, count in Counter(causes).items() if count > 1
    }
    failure_patterns = [
        {
            "pattern_hash": record.get("failure_pattern_hash"),
            "scenario_config_hash": record.get("scenario_config_hash"),
            "cause": record.get("cause"),
            "recommendation": record.get("refiner_recommendation"),
            "fix_type": record.get("refiner_fix_type"),
            "run_id": record.get("run_id"),
        }
        for record in records
        if record.get("failure_pattern_hash")
    ]
    return {
        "total_runs": len(records),
        "best_run_id": best.get("run_id") if best else None,
        "best_objective_value_usd": best.get("objective_value_usd") if best else None,
        "convergence_trend": trend,
        "improvement_pct_last": improvement_pct_last(trend),
        "improvement_pct_total": improvement_pct_total(trend),
        "plateau_detected": plateau_detected(trend),
        "known_failure_patterns": failure_patterns,
        "repeated_causes": repeated_causes,
    }


def read_history(history_path: Path) -> list[dict[str, Any]]:
    if not history_path.exists():
        return []
    records = []
    with history_path.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                records.append(value)
    return records


def last_item(value: Any) -> dict[str, Any]:
    if isinstance(value, list) and value and isinstance(value[-1], dict):
        return value[-1]
    return {}


def failure_pattern_hash(config_hash: str | None, cause: str | None) -> str | None:
    if not config_hash or not cause:
        return None
    import hashlib

    return hashlib.sha256(f"{config_hash}:{cause}".encode("utf-8")).hexdigest()


def improvement_pct_last(trend: list[dict[str, Any]]) -> float | None:
    if len(trend) < 2:
        return None
    previous = trend[-2].get("objective_value_usd")
    current = trend[-1].get("objective_value_usd")
    if not previous or current is None:
        return None
    return (float(previous) - float(current)) / float(previous) * 100.0


def improvement_pct_total(trend: list[dict[str, Any]]) -> float | None:
    if len(trend) < 2:
        return None
    first = trend[0].get("objective_value_usd")
    current = trend[-1].get("objective_value_usd")
    if not first or current is None:
        return None
    return (float(first) - float(current)) / float(first) * 100.0


def plateau_detected(trend: list[dict[str, Any]]) -> bool:
    if len(trend) < 4:
        return False
    improvements = []
    for index in range(-3, 0):
        previous = trend[index - 1].get("objective_value_usd")
        current = trend[index].get("objective_value_usd")
        if not previous or current is None:
            return False
        improvements.append(abs(float(previous) - float(current)) / float(previous))
    return all(value < 0.001 for value in improvements)
