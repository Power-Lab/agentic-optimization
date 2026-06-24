from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import pandas as pd

from agent.validation import normalized_input_files, resolve_repo_path


BALANCE_TOLERANCE_MWH = 1.0e-5
CAPACITY_TOLERANCE_MWH = 1.0e-5
COST_TOLERANCE_USD = 1.0e-2
NSE_WARN_MWH = 1.0
NSE_ANOMALY_DEMAND_FRACTION = 0.01


@dataclass
class AnalysisIssue:
    severity: str
    code: str
    message: str
    path: str | None = None


@dataclass
class OutputAnalysisResult:
    overall_status: str
    checks: list[AnalysisIssue] = field(default_factory=list)
    metrics: dict[str, Any] = field(default_factory=dict)
    recommendations: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "overall_status": self.overall_status,
            "checks": [asdict(check) for check in self.checks],
            "metrics": self.metrics,
            "recommendations": self.recommendations,
        }


def analyze_outputs(config: dict[str, Any], repo_root: Path, solve_summary: dict[str, Any] | None) -> OutputAnalysisResult:
    result = OutputAnalysisResult(overall_status="OK")
    if solve_summary is None:
        result.checks.append(
            AnalysisIssue(
                severity="ANOMALY",
                code="MISSING_SOLVE_SUMMARY",
                message="Model completed but no solve summary was found.",
            )
        )
        finalize(result)
        return result

    validate_summary_status(result, solve_summary)
    validate_cost_components(result, solve_summary)

    paths = output_paths(config, repo_root)
    validate_output_paths(result, paths)
    if any(issue.severity == "ANOMALY" for issue in result.checks):
        finalize(result)
        return result

    try:
        capacity = normalize_capacity(pd.read_csv(paths["capacity"]))
        dispatch = normalize_dispatch(pd.read_csv(paths["dispatch"]))
        nse = normalize_nse(pd.read_csv(paths["nse"]))
        demand = normalize_demand(read_demand(config, repo_root))
    except Exception as exc:
        result.checks.append(
            AnalysisIssue(
                severity="ANOMALY",
                code="OUTPUT_READ_FAILED",
                message=f"Could not read or normalize output files: {exc}",
            )
        )
        finalize(result)
        return result

    analyze_capacity_nonnegative(result, capacity)
    analyze_power_balance(result, dispatch, nse, demand)
    analyze_capacity_limits(result, dispatch, capacity)
    analyze_nse(result, nse, demand)
    finalize(result)
    return result


def validate_summary_status(result: OutputAnalysisResult, summary: dict[str, Any]) -> None:
    status = str(summary.get("termination_status", "")).upper()
    result.metrics["termination_status"] = status
    if status not in {"OPTIMAL", "FEASIBLE_POINT"}:
        result.checks.append(
            AnalysisIssue(
                severity="ANOMALY",
                code="UNACCEPTED_TERMINATION_STATUS",
                message=f"Termination status {status or '<missing>'} is not accepted.",
            )
        )


def validate_cost_components(result: OutputAnalysisResult, summary: dict[str, Any]) -> None:
    required = [
        "objective_value_usd",
        "total_fixed_cost_usd",
        "total_variable_cost_usd",
        "total_nse_penalty_usd",
    ]
    if any(key not in summary for key in required):
        result.checks.append(
            AnalysisIssue(
                severity="WARN",
                code="MISSING_COST_COMPONENTS",
                message="Solve summary does not include all cost components needed for objective cross-check.",
            )
        )
        return

    objective = float(summary["objective_value_usd"])
    component_sum = (
        float(summary["total_fixed_cost_usd"])
        + float(summary["total_variable_cost_usd"])
        + float(summary["total_nse_penalty_usd"])
    )
    result.metrics["objective_value_usd"] = objective
    result.metrics["reported_cost_component_sum_usd"] = component_sum
    result.metrics["objective_component_gap_usd"] = abs(objective - component_sum)
    if abs(objective - component_sum) > max(COST_TOLERANCE_USD, abs(objective) * 1.0e-8):
        result.checks.append(
            AnalysisIssue(
                severity="ANOMALY",
                code="OBJECTIVE_COST_MISMATCH",
                message=f"Objective {objective} does not match cost component sum {component_sum}.",
            )
        )


def output_paths(config: dict[str, Any], repo_root: Path) -> dict[str, Path]:
    outputs = config.get("expected_output_files", {})
    if not isinstance(outputs, dict):
        outputs = {}
    capacity_spec = outputs.get("capacity_build", {})
    dispatch_spec = outputs.get("hourly_dispatch") or outputs.get("hourly_generation") or {}
    nse_spec = outputs.get("non_served_energy", {})
    summary_spec = outputs.get("solve_summary", {})
    return {
        "capacity": resolve_output_path(repo_root, capacity_spec),
        "dispatch": resolve_output_path(repo_root, dispatch_spec),
        "nse": resolve_output_path(repo_root, nse_spec),
        "summary": resolve_output_path(repo_root, summary_spec),
    }


def resolve_output_path(repo_root: Path, spec: Any) -> Path:
    if not isinstance(spec, dict) or not spec.get("path"):
        return repo_root / "__missing_output_path__"
    return resolve_repo_path(repo_root, str(spec["path"]))


def validate_output_paths(result: OutputAnalysisResult, paths: dict[str, Path]) -> None:
    for name, path in paths.items():
        if not path.exists():
            result.checks.append(
                AnalysisIssue(
                    severity="ANOMALY",
                    code="OUTPUT_FILE_NOT_FOUND",
                    message=f"Expected {name} output file does not exist.",
                    path=str(path),
                )
            )


def read_demand(config: dict[str, Any], repo_root: Path) -> pd.DataFrame:
    for name, spec in normalized_input_files(config).items():
        if "demand" in name.lower() and spec.get("path"):
            return pd.read_csv(resolve_repo_path(repo_root, str(spec["path"])))
    raise ValueError("No demand input file declared in scenario config.")


def normalize_capacity(frame: pd.DataFrame) -> pd.DataFrame:
    label = first_column(frame, ["technology", "G", "Description"])
    capacity = first_column(frame, ["build_capacity_mw", "capacity_mw"])
    normalized = frame[[label, capacity]].copy()
    normalized.columns = ["generator", "capacity_mw"]
    normalized["generator"] = normalized["generator"].astype(str)
    normalized["capacity_mw"] = pd.to_numeric(normalized["capacity_mw"], errors="raise")
    return normalized


def normalize_dispatch(frame: pd.DataFrame) -> pd.DataFrame:
    hour = first_column(frame, ["hour", "Hour", "H"])
    label = first_column(frame, ["technology", "G", "Description"])
    generation = first_column(frame, ["generation_mwh", "GEN", "dispatch_mwh"])
    normalized = frame[[hour, label, generation]].copy()
    normalized.columns = ["hour", "generator", "generation_mwh"]
    normalized["hour"] = pd.to_numeric(normalized["hour"], errors="raise").astype(int)
    normalized["generator"] = normalized["generator"].astype(str)
    normalized["generation_mwh"] = pd.to_numeric(normalized["generation_mwh"], errors="raise")
    return normalized


def normalize_nse(frame: pd.DataFrame) -> pd.DataFrame:
    hour = first_column(frame, ["hour", "Hour", "H"])
    nse = first_column(frame, ["non_served_energy_mwh", "nse_mwh", "NSE"])
    normalized = frame[[hour, nse]].copy()
    normalized.columns = ["hour", "nse_mwh"]
    normalized["hour"] = pd.to_numeric(normalized["hour"], errors="raise").astype(int)
    normalized["nse_mwh"] = pd.to_numeric(normalized["nse_mwh"], errors="raise")
    return normalized


def normalize_demand(frame: pd.DataFrame) -> pd.DataFrame:
    hour = first_column(frame, ["hour", "Hour", "H"])
    demand = first_column(frame, ["demand_mw", "Demand", "demand"])
    normalized = frame[[hour, demand]].copy()
    normalized.columns = ["hour", "demand_mwh"]
    normalized["hour"] = pd.to_numeric(normalized["hour"], errors="raise").astype(int)
    normalized["demand_mwh"] = pd.to_numeric(normalized["demand_mwh"], errors="raise")
    return normalized


def first_column(frame: pd.DataFrame, candidates: list[str]) -> str:
    for candidate in candidates:
        if candidate in frame.columns:
            return candidate
    raise ValueError(f"Missing one of columns {candidates}; found {list(frame.columns)}")


def analyze_capacity_nonnegative(result: OutputAnalysisResult, capacity: pd.DataFrame) -> None:
    min_capacity = float(capacity["capacity_mw"].min()) if not capacity.empty else 0.0
    result.metrics["min_capacity_mw"] = min_capacity
    if min_capacity < -CAPACITY_TOLERANCE_MWH:
        result.checks.append(
            AnalysisIssue(
                severity="ANOMALY",
                code="NEGATIVE_CAPACITY",
                message=f"Capacity output includes negative capacity: {min_capacity}.",
            )
        )


def analyze_power_balance(
    result: OutputAnalysisResult,
    dispatch: pd.DataFrame,
    nse: pd.DataFrame,
    demand: pd.DataFrame,
) -> None:
    generation = dispatch.groupby("hour", as_index=False)["generation_mwh"].sum()
    merged = demand.merge(generation, on="hour", how="left").merge(nse, on="hour", how="left")
    merged[["generation_mwh", "nse_mwh"]] = merged[["generation_mwh", "nse_mwh"]].fillna(0.0)
    merged["imbalance_mwh"] = (merged["generation_mwh"] + merged["nse_mwh"] - merged["demand_mwh"]).abs()
    max_imbalance = float(merged["imbalance_mwh"].max())
    result.metrics["max_power_balance_imbalance_mwh"] = max_imbalance
    result.metrics["total_demand_mwh"] = float(merged["demand_mwh"].sum())
    if max_imbalance > BALANCE_TOLERANCE_MWH:
        worst = merged.loc[merged["imbalance_mwh"].idxmax()]
        result.checks.append(
            AnalysisIssue(
                severity="ANOMALY",
                code="POWER_BALANCE_VIOLATION",
                message=(
                    f"Max hourly imbalance is {max_imbalance} MWh at hour "
                    f"{int(worst['hour'])}."
                ),
            )
        )


def analyze_capacity_limits(
    result: OutputAnalysisResult,
    dispatch: pd.DataFrame,
    capacity: pd.DataFrame,
) -> None:
    merged = dispatch.merge(capacity, on="generator", how="left")
    if merged["capacity_mw"].isna().any():
        missing = sorted(merged.loc[merged["capacity_mw"].isna(), "generator"].unique().tolist())
        result.checks.append(
            AnalysisIssue(
                severity="ANOMALY",
                code="MISSING_GENERATOR_CAPACITY",
                message=f"Dispatch contains generators without capacity rows: {missing}",
            )
        )
        return

    merged["capacity_violation_mwh"] = merged["generation_mwh"] - merged["capacity_mw"]
    max_violation = float(merged["capacity_violation_mwh"].max())
    result.metrics["max_capacity_violation_mwh"] = max(max_violation, 0.0)
    if max_violation > CAPACITY_TOLERANCE_MWH:
        worst = merged.loc[merged["capacity_violation_mwh"].idxmax()]
        result.checks.append(
            AnalysisIssue(
                severity="ANOMALY",
                code="CAPACITY_LIMIT_VIOLATION",
                message=(
                    f"Generator {worst['generator']} produced {worst['generation_mwh']} MWh "
                    f"against {worst['capacity_mw']} MW capacity at hour {int(worst['hour'])}."
                ),
            )
        )


def analyze_nse(result: OutputAnalysisResult, nse: pd.DataFrame, demand: pd.DataFrame) -> None:
    total_nse = float(nse["nse_mwh"].sum())
    total_demand = float(demand["demand_mwh"].sum())
    nse_fraction = total_nse / total_demand if total_demand else 0.0
    result.metrics["total_nse_mwh"] = total_nse
    result.metrics["nse_fraction_of_demand"] = nse_fraction
    if total_nse < -BALANCE_TOLERANCE_MWH:
        result.checks.append(
            AnalysisIssue(
                severity="ANOMALY",
                code="NEGATIVE_NSE",
                message=f"Non-served energy is negative: {total_nse}.",
            )
        )
    elif nse_fraction > NSE_ANOMALY_DEMAND_FRACTION:
        result.checks.append(
            AnalysisIssue(
                severity="ANOMALY",
                code="MATERIAL_LOAD_SHEDDING",
                message=f"Total NSE is {total_nse} MWh, {nse_fraction:.4%} of demand.",
            )
        )
    elif total_nse > NSE_WARN_MWH:
        result.checks.append(
            AnalysisIssue(
                severity="WARN",
                code="LOAD_SHEDDING_PRESENT",
                message=f"Total NSE is {total_nse} MWh.",
            )
        )


def finalize(result: OutputAnalysisResult) -> None:
    severities = {issue.severity for issue in result.checks}
    if "ANOMALY" in severities:
        result.overall_status = "ANOMALY"
        result.recommendations.append("Critical output anomalies detected; route to refiner before accepting.")
    elif "WARN" in severities:
        result.overall_status = "WARN"
        result.recommendations.append("Warnings detected; solution is usable only with review.")
    else:
        result.overall_status = "OK"
        result.recommendations.append("All output checks passed.")
