from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class RefinementResult:
    recommendation: str
    fix_type: str
    confidence: str
    trigger_context: str
    cause: str
    refinement_overrides: dict[str, Any] = field(default_factory=dict)
    structural_change_request: str | None = None
    explanation: str = ""
    source_issues: list[dict[str, Any]] = field(default_factory=list)
    fix_history_entry: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def refine_run(
    run_id: str,
    iteration: int,
    config: dict[str, Any],
    validation_result: dict[str, Any] | None = None,
    solver_log_result: dict[str, Any] | None = None,
    output_analysis_result: dict[str, Any] | None = None,
    executor_result: dict[str, Any] | None = None,
) -> RefinementResult:
    context, cause, issues = identify_primary_issue(
        validation_result,
        solver_log_result,
        output_analysis_result,
        executor_result,
    )
    result = build_recommendation(context, cause, config, issues)
    result.fix_history_entry = {
        "run_id": run_id,
        "iteration": iteration,
        "trigger": context,
        "cause": cause,
        "fix_type": result.fix_type,
        "recommendation": result.recommendation,
    }
    return result


def identify_primary_issue(
    validation_result: dict[str, Any] | None,
    solver_log_result: dict[str, Any] | None,
    output_analysis_result: dict[str, Any] | None,
    executor_result: dict[str, Any] | None,
) -> tuple[str, str, list[dict[str, Any]]]:
    validation_issues = issue_list(validation_result, "checks")
    solver_issues = issue_list(solver_log_result, "checks")
    output_issues = issue_list(output_analysis_result, "checks")

    validation_status = validation_result.get("status") if validation_result else None
    solver_status = solver_log_result.get("overall_status") if solver_log_result else None
    output_status = output_analysis_result.get("overall_status") if output_analysis_result else None
    executor_status = executor_result.get("status") if executor_result else None

    if validation_status == "FAIL":
        return "VALIDATION_FAILURE", first_code(validation_issues, "SCENARIO_VALIDATION_FAILED"), validation_issues

    if executor_status and executor_status != "COMPLETED":
        return "EXECUTOR_FAILURE", str(executor_result.get("error") or executor_status), []

    if solver_status == "INFEASIBLE":
        infeasibility = solver_log_result.get("infeasibility", {}) if solver_log_result else {}
        return "INFEASIBILITY", str(infeasibility.get("likely_cause") or "UNKNOWN_INFEASIBILITY"), solver_issues

    if solver_status == "NUMERICAL_ERROR":
        return "NUMERICAL_INSTABILITY", first_code(solver_issues, "NUMERICAL_ERROR"), solver_issues

    if solver_status == "TIMEOUT_SUBOPTIMAL":
        return "TIMEOUT", first_code(solver_issues, "TIME_LIMIT_REACHED"), solver_issues

    if output_status == "ANOMALY":
        return "OUTPUT_ANOMALY", first_code(output_issues, "OUTPUT_ANOMALY"), output_issues

    if output_status == "WARN":
        return "OUTPUT_WARN", first_code(output_issues, "OUTPUT_WARN"), output_issues

    if solver_status == "WARN":
        return "SOLVER_LOG_WARN", first_code(solver_issues, "SOLVER_LOG_WARN"), solver_issues

    return "NO_ISSUE", "NONE", []


def issue_list(result: dict[str, Any] | None, key: str) -> list[dict[str, Any]]:
    if not isinstance(result, dict):
        return []
    issues = result.get(key)
    return issues if isinstance(issues, list) else []


def first_code(issues: list[dict[str, Any]], fallback: str) -> str:
    for issue in issues:
        code = issue.get("code")
        if code:
            return str(code)
    return fallback


def build_recommendation(
    context: str,
    cause: str,
    config: dict[str, Any],
    issues: list[dict[str, Any]],
) -> RefinementResult:
    if context == "VALIDATION_FAILURE":
        return validation_recommendation(cause, issues)
    if context == "EXECUTOR_FAILURE":
        return RefinementResult(
            recommendation="RESTRUCTURE",
            fix_type="MODEL_REFORMULATION",
            confidence="LOW",
            trigger_context=context,
            cause=cause,
            explanation="The model executor failed before a trustworthy solver result was available. Inspect the command logs and repair the execution boundary before rerunning.",
            source_issues=issues,
        )
    if context == "INFEASIBILITY":
        return infeasibility_recommendation(cause, issues)
    if context == "NUMERICAL_INSTABILITY":
        return numerical_recommendation(cause, issues)
    if context == "TIMEOUT":
        return timeout_recommendation(config, issues)
    if context == "OUTPUT_ANOMALY":
        return output_anomaly_recommendation(cause, issues)
    if context == "OUTPUT_WARN":
        return output_warn_recommendation(cause, issues)
    if context == "SOLVER_LOG_WARN":
        return solver_warn_recommendation(cause, issues)
    return RefinementResult(
        recommendation="STOP",
        fix_type="NO_FIX",
        confidence="HIGH",
        trigger_context=context,
        cause=cause,
        explanation="No refinement is needed.",
        source_issues=issues,
    )


def validation_recommendation(cause: str, issues: list[dict[str, Any]]) -> RefinementResult:
    path_codes = {"INPUT_FILE_NOT_FOUND", "MISSING_INPUT_FILE_PATH", "MISSING_DEMAND_INPUT", "MISSING_GENERATOR_INPUT"}
    if cause in path_codes:
        explanation = "Scenario validation failed because required data paths are missing or invalid. Correct the scenario input paths before rerunning."
        fix_type = "CONFIG_REPAIR"
        confidence = "HIGH"
    elif cause in {"CANDIDATE_FILTER_MATCHED_NOTHING", "EMPTY_CANDIDATE_FILTER", "MISSING_CANDIDATE_FILTER"}:
        explanation = "The candidate generator filter did not select a valid fleet. Correct include values or the technology column mapping before rerunning."
        fix_type = "CONFIG_REPAIR"
        confidence = "HIGH"
    else:
        explanation = "Scenario validation failed. Repair the listed schema or input-data issues before launching the solver."
        fix_type = "CONFIG_REPAIR"
        confidence = "MEDIUM"
    return RefinementResult(
        recommendation="CONTINUE",
        fix_type=fix_type,
        confidence=confidence,
        trigger_context="VALIDATION_FAILURE",
        cause=cause,
        explanation=explanation,
        source_issues=issues,
    )


def infeasibility_recommendation(cause: str, issues: list[dict[str, Any]]) -> RefinementResult:
    if cause == "EMISSION_CAP_TOO_TIGHT":
        return RefinementResult(
            recommendation="CONTINUE",
            fix_type="PARAMETER_RELAXATION",
            confidence="HIGH",
            trigger_context="INFEASIBILITY",
            cause=cause,
            refinement_overrides={"policy_constraints": {"emission_cap_tCO2": "RELAX_BY_20_PERCENT"}},
            explanation="The infeasibility maps to a tight emissions cap. Relax the cap by 20% for the next iteration.",
            source_issues=issues,
        )
    if cause == "DEMAND_SUPPLY_MISMATCH":
        return RefinementResult(
            recommendation="RESTRUCTURE",
            fix_type="STRUCTURAL_CHANGE",
            confidence="HIGH",
            trigger_context="INFEASIBILITY",
            cause=cause,
            structural_change_request="Add dispatchable peaking capacity or enable non-served energy with a high penalty.",
            explanation="Power-balance infeasibility usually means available capacity cannot meet demand. This needs a scenario rebuild, not just solver tuning.",
            source_issues=issues,
        )
    return RefinementResult(
        recommendation="RESTRUCTURE",
        fix_type="STRUCTURAL_CHANGE",
        confidence="LOW",
        trigger_context="INFEASIBILITY",
        cause=cause,
        structural_change_request="Review IIS details and rebuild the scenario around the binding constraints.",
        explanation="The infeasibility cause is not specific enough for a safe parameter-only fix.",
        source_issues=issues,
    )


def numerical_recommendation(cause: str, issues: list[dict[str, Any]]) -> RefinementResult:
    return RefinementResult(
        recommendation="CONTINUE",
        fix_type="SOLVER_TUNING",
        confidence="MEDIUM",
        trigger_context="NUMERICAL_INSTABILITY",
        cause=cause,
        refinement_overrides={"solver_settings": {"NumericFocus": 2, "ScaleFlag": 2}},
        explanation="Numerical diagnostics indicate the next run should use more conservative scaling and numerical focus settings.",
        source_issues=issues,
    )


def timeout_recommendation(config: dict[str, Any], issues: list[dict[str, Any]]) -> RefinementResult:
    settings = config.get("solver_settings", {}) if isinstance(config.get("solver_settings"), dict) else {}
    current_limit = settings.get("time_limit_seconds") or 600
    new_limit = float(current_limit) * 1.5
    return RefinementResult(
        recommendation="CONTINUE",
        fix_type="SOLVER_TUNING",
        confidence="HIGH",
        trigger_context="TIMEOUT",
        cause="TIME_LIMIT_REACHED",
        refinement_overrides={"solver_settings": {"time_limit_seconds": new_limit, "warm_start": True}},
        explanation=f"Solver timed out. Increase time limit from {current_limit} to {new_limit:g} seconds and enable warm start if a feasible incumbent exists.",
        source_issues=issues,
    )


def output_anomaly_recommendation(cause: str, issues: list[dict[str, Any]]) -> RefinementResult:
    if cause == "POWER_BALANCE_VIOLATION":
        return RefinementResult(
            recommendation="RESTRUCTURE",
            fix_type="MODEL_REFORMULATION",
            confidence="HIGH",
            trigger_context="OUTPUT_ANOMALY",
            cause=cause,
            structural_change_request="Audit demand-balance constraints and output parsing before rerunning.",
            explanation="Power balance violations indicate a model construction or output parsing error; do not accept the solution.",
            source_issues=issues,
        )
    return RefinementResult(
        recommendation="RESTRUCTURE",
        fix_type="MODEL_REFORMULATION",
        confidence="MEDIUM",
        trigger_context="OUTPUT_ANOMALY",
        cause=cause,
        structural_change_request="Audit the model constraint family associated with the anomaly.",
        explanation="Output analysis found an anomaly that should be corrected before rerunning.",
        source_issues=issues,
    )


def output_warn_recommendation(cause: str, issues: list[dict[str, Any]]) -> RefinementResult:
    if cause == "LOAD_SHEDDING_PRESENT":
        explanation = "Load shedding is present but below the anomaly threshold. Review capacity limits, build candidates, and NSE penalty before using the result for planning decisions."
    else:
        explanation = "Output analysis warnings are present but not severe enough to force a rerun. Review diagnostics and stop unless the warning is unacceptable for the study."
    return RefinementResult(
        recommendation="STOP",
        fix_type="NO_FIX",
        confidence="MEDIUM",
        trigger_context="OUTPUT_WARN",
        cause=cause,
        explanation=explanation,
        source_issues=issues,
    )


def solver_warn_recommendation(cause: str, issues: list[dict[str, Any]]) -> RefinementResult:
    if cause == "NOT_REDUCED_BY_PRESOLVE":
        explanation = "Presolve did not reduce the model. This is a performance warning, not a correctness failure for this LP."
    elif cause == "DECLARED_SOLVER_LOG_NOT_FOUND":
        explanation = "The configured solver log path was missing. Configure the model runner to write logs to the declared path for stronger diagnostics."
    else:
        explanation = "Solver log warnings are present but do not block output analysis. Review them before treating the run as final."
    return RefinementResult(
        recommendation="STOP",
        fix_type="NO_FIX",
        confidence="MEDIUM",
        trigger_context="SOLVER_LOG_WARN",
        cause=cause,
        explanation=explanation,
        source_issues=issues,
    )
