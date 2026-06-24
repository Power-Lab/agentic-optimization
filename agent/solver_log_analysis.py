from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from agent.validation import resolve_repo_path


@dataclass
class SolverLogIssue:
    severity: str
    code: str
    message: str
    path: str | None = None


@dataclass
class SolverLogAnalysisResult:
    overall_status: str
    log_path: str | None = None
    termination_status: str | None = None
    infeasibility: dict[str, Any] = field(default_factory=dict)
    convergence: dict[str, Any] = field(default_factory=dict)
    numerical_issues: dict[str, Any] = field(default_factory=dict)
    performance: dict[str, Any] = field(default_factory=dict)
    checks: list[SolverLogIssue] = field(default_factory=list)
    recommendations: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "overall_status": self.overall_status,
            "log_path": self.log_path,
            "termination_status": self.termination_status,
            "infeasibility": self.infeasibility,
            "convergence": self.convergence,
            "numerical_issues": self.numerical_issues,
            "performance": self.performance,
            "checks": [asdict(check) for check in self.checks],
            "recommendations": self.recommendations,
        }


def analyze_solver_log(
    config: dict[str, Any],
    repo_root: Path,
    executor_result: dict[str, Any],
) -> SolverLogAnalysisResult:
    result = SolverLogAnalysisResult(
        overall_status="OK",
        infeasibility={"detected": False, "type": None, "likely_cause": None},
        convergence={"converged": False, "final_mip_gap": None, "convergence_rate": "N/A"},
        numerical_issues={"detected": False, "patterns": []},
        performance={},
    )

    text, log_path = load_log_text(config, repo_root, executor_result, result)
    result.log_path = str(log_path) if log_path else None
    normalized = normalize_log_text(text)

    summary = executor_result.get("solve_summary") if isinstance(executor_result, dict) else None
    if not isinstance(summary, dict):
        summary = {}

    detect_termination(result, normalized, summary, executor_result)
    detect_infeasibility(result, normalized)
    detect_numerical_issues(result, normalized)
    extract_performance(result, normalized, summary, executor_result)
    classify_convergence(result)
    finalize(result)
    return result


def load_log_text(
    config: dict[str, Any],
    repo_root: Path,
    executor_result: dict[str, Any],
    result: SolverLogAnalysisResult,
) -> tuple[str, Path | None]:
    configured_log = configured_log_path(config, repo_root)
    if configured_log and configured_log.exists():
        return configured_log.read_text(encoding="utf-8", errors="replace"), configured_log

    if configured_log:
        result.checks.append(
            SolverLogIssue(
                severity="WARN",
                code="DECLARED_SOLVER_LOG_NOT_FOUND",
                message="Configured solver log file was not found; using captured stdout/stderr fallback.",
                path=str(configured_log),
            )
        )

    chunks = []
    for key in ("stdout_log", "stderr_log"):
        raw_path = executor_result.get(key)
        if not raw_path:
            continue
        path = Path(str(raw_path))
        if path.exists():
            chunks.append(path.read_text(encoding="utf-8", errors="replace"))

    if chunks:
        return "\n".join(chunks), None

    result.checks.append(
        SolverLogIssue(
            severity="WARN",
            code="SOLVER_LOG_FALLBACK_EMPTY",
            message="No solver log text was available; relying on executor summary only.",
        )
    )
    return "", None


def configured_log_path(config: dict[str, Any], repo_root: Path) -> Path | None:
    settings = config.get("solver_settings")
    if not isinstance(settings, dict) or not settings.get("log_file"):
        return None
    return resolve_repo_path(repo_root, str(settings["log_file"]))


def normalize_log_text(text: str) -> str:
    text = re.sub(r"\x1b\[[0-9;]*m", "", text)
    return "\n".join(line.strip() for line in text.splitlines())


def detect_termination(
    result: SolverLogAnalysisResult,
    text: str,
    summary: dict[str, Any],
    executor_result: dict[str, Any],
) -> None:
    lowered = text.lower()
    summary_status = str(summary.get("termination_status", "")).upper()
    executor_status = str(executor_result.get("status", "")).upper()

    if "model status" in lowered and "optimal" in lowered:
        result.termination_status = "OPTIMAL"
    elif "optimal solution found" in lowered or "optimization terminated successfully" in lowered:
        result.termination_status = "OPTIMAL"
    elif "infeasible or unbounded" in lowered:
        result.termination_status = "INFEASIBLE_OR_UNBOUNDED"
    elif "infeasible" in lowered:
        result.termination_status = "INFEASIBLE"
    elif "time limit" in lowered:
        result.termination_status = "TIME_LIMIT"
    elif "unbounded" in lowered:
        result.termination_status = "UNBOUNDED"
    elif summary_status:
        result.termination_status = summary_status
    elif executor_status == "COMPLETED":
        result.termination_status = "UNKNOWN_COMPLETED"
    else:
        result.termination_status = executor_status or "UNKNOWN"


def detect_infeasibility(result: SolverLogAnalysisResult, text: str) -> None:
    lowered = text.lower()
    if result.termination_status in {"INFEASIBLE", "INFEASIBLE_OR_UNBOUNDED"}:
        infeasible_type = "INFEASIBLE_OR_UNBOUNDED"
        if result.termination_status == "INFEASIBLE":
            infeasible_type = "PRIMAL_INFEASIBLE"
        result.infeasibility.update({"detected": True, "type": infeasible_type})
        result.checks.append(
            SolverLogIssue(
                severity="INFEASIBLE",
                code=result.termination_status,
                message=f"Solver reported {result.termination_status}.",
            )
        )

    if result.infeasibility.get("detected"):
        likely_cause = None
        if "emission_cap" in lowered:
            likely_cause = "EMISSION_CAP_TOO_TIGHT"
        elif "power_balance" in lowered:
            likely_cause = "DEMAND_SUPPLY_MISMATCH"
        elif "renewable_penetration" in lowered:
            likely_cause = "RENEWABLE_TARGET_UNREACHABLE"
        elif re.search(r"\bramp(_up|_down)?\b", lowered):
            likely_cause = "COMMITMENT_RAMP_INFEASIBILITY"
        if likely_cause:
            result.infeasibility["likely_cause"] = likely_cause


def detect_numerical_issues(result: SolverLogAnalysisResult, text: str) -> None:
    patterns = {
        "PRIMAL_DUAL_DISAGREEMENT": "primal objective disagree",
        "NUMERICAL_WARNING": "numerical",
        "LARGE_COEFFICIENTS": "large coefficient",
        "NOT_REDUCED_BY_PRESOLVE": "not reduced by presolve",
    }
    lowered = text.lower()
    found = []
    for code, pattern in patterns.items():
        if pattern in lowered:
            found.append(code)
            severity = "WARN"
            if code in {"PRIMAL_DUAL_DISAGREEMENT", "NUMERICAL_WARNING"}:
                severity = "NUMERICAL_ERROR"
            result.checks.append(
                SolverLogIssue(
                    severity=severity,
                    code=code,
                    message=f"Solver log contains pattern: {pattern}",
                )
            )
    if re.search(r"warning:.*coefficient", lowered):
        found.append("COEFFICIENT_RANGE_WARNING")
        result.checks.append(
            SolverLogIssue(
                severity="WARN",
                code="COEFFICIENT_RANGE_WARNING",
                message="Solver log contains a coefficient warning.",
            )
        )
    result.numerical_issues = {"detected": bool(found), "patterns": found}


def extract_performance(
    result: SolverLogAnalysisResult,
    text: str,
    summary: dict[str, Any],
    executor_result: dict[str, Any],
) -> None:
    objective = summary.get("objective_value_usd")
    if objective is not None:
        result.performance["objective_value_usd"] = float(objective)
    if executor_result.get("elapsed_s") is not None:
        result.performance["executor_elapsed_s"] = float(executor_result["elapsed_s"])

    highs_time = re.search(r"HiGHS run time\s*:\s*([0-9.]+)", text)
    if highs_time:
        result.performance["solver_runtime_s"] = float(highs_time.group(1))

    simplex_iterations = re.search(r"Simplex\s+iterations:\s*(\d+)", text)
    if simplex_iterations:
        result.performance["simplex_iterations"] = int(simplex_iterations.group(1))

    rows_cols = re.search(r"LP has\s+(\d+)\s+rows;\s+(\d+)\s+cols;\s+(\d+)\s+nonzeros", text)
    if rows_cols:
        result.performance["rows"] = int(rows_cols.group(1))
        result.performance["columns"] = int(rows_cols.group(2))
        result.performance["nonzeros"] = int(rows_cols.group(3))

    objective_match = re.search(r"Objective value\s*:\s*([0-9.eE+-]+)", text)
    if objective_match:
        result.performance["log_objective_value"] = float(objective_match.group(1))


def classify_convergence(result: SolverLogAnalysisResult) -> None:
    if result.termination_status in {"OPTIMAL", "FEASIBLE_POINT"}:
        result.convergence.update({"converged": True, "convergence_rate": "NORMAL"})
    elif result.termination_status == "TIME_LIMIT":
        result.convergence.update({"converged": False, "convergence_rate": "TIME_LIMIT"})
        result.checks.append(
            SolverLogIssue(
                severity="TIMEOUT_SUBOPTIMAL",
                code="TIME_LIMIT_REACHED",
                message="Solver reached the time limit.",
            )
        )
    else:
        result.convergence.update({"converged": False, "convergence_rate": "N/A"})


def finalize(result: SolverLogAnalysisResult) -> None:
    severities = {issue.severity for issue in result.checks}
    if "INFEASIBLE" in severities:
        result.overall_status = "INFEASIBLE"
        result.recommendations.append("Route infeasible scenario to the refiner before output analysis.")
    elif "NUMERICAL_ERROR" in severities:
        result.overall_status = "NUMERICAL_ERROR"
        result.recommendations.append("Do not trust this solution until numerical issues are resolved.")
    elif "TIMEOUT_SUBOPTIMAL" in severities:
        result.overall_status = "TIMEOUT_SUBOPTIMAL"
        result.recommendations.append("Route timeout to refiner or rerun with adjusted solver settings.")
    elif "WARN" in severities:
        result.overall_status = "WARN"
        result.recommendations.append("Solver log warnings detected; continue with output analysis but review diagnostics.")
    else:
        result.overall_status = "OK"
        result.recommendations.append("Solver log is clean enough to proceed to output analysis.")
