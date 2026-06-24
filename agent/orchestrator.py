from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from agent.iteration_memory import append_history_record, summarize_history
from agent.output_analysis import analyze_outputs
from agent.refiner import refine_run
from agent.scenario_patching import create_safe_patched_config
from agent.solver_log_analysis import analyze_solver_log
from agent.validation import validate_scenario_config


ACTION_TRIGGER_REFINER = "TRIGGER_REFINER"
ACTION_ANALYZE_SOLVER_LOG = "ANALYZE_SOLVER_LOG"
ACTION_ANALYZE_OUTPUTS = "ANALYZE_OUTPUTS"
ACTION_VALIDATE_SCENARIO = "VALIDATE_SCENARIO"
ACTION_LAUNCH_SOLVER = "LAUNCH_SOLVER"
ACTION_TERMINATE_FAILURE = "TERMINATE_FAILURE"
ACTION_TERMINATE_SUCCESS = "TERMINATE_SUCCESS"


@dataclass
class IterationState:
    current_iteration: int = 0
    max_iterations: int = 1
    elapsed_wall_time_s: float = 0.0
    wall_time_budget_s: float = 7200.0
    restructure_count: int = 0


@dataclass
class SolverHints:
    mip_gap: float | None = None
    time_limit_s: float | None = None
    warm_start: bool = False


@dataclass
class ControllerDecision:
    run_id: str
    iteration: int
    action: str
    reason: str
    next_skill: str | None
    updated_solver_hints: SolverHints
    iteration_budget_remaining: int
    wall_time_remaining_s: float
    termination_criteria_met: bool
    log_entry: dict[str, Any]


@dataclass
class ExecutorResult:
    status: str
    command: list[str]
    return_code: int
    stdout_log: str
    stderr_log: str
    elapsed_s: float
    solve_summary_path: str | None = None
    solve_summary: dict[str, Any] | None = None
    error: str | None = None


@dataclass
class RunState:
    run_id: str
    scenario_id: str
    scenario_config_path: str
    scenario_config_hash: str
    run_dir: str
    created_at: str
    status: str = "PENDING"
    iteration_state: IterationState = field(default_factory=IterationState)
    controller_decisions: list[dict[str, Any]] = field(default_factory=list)
    validation_results: list[dict[str, Any]] = field(default_factory=list)
    executor_results: list[dict[str, Any]] = field(default_factory=list)
    solver_log_analysis_results: list[dict[str, Any]] = field(default_factory=list)
    output_analysis_results: list[dict[str, Any]] = field(default_factory=list)
    refinement_results: list[dict[str, Any]] = field(default_factory=list)
    patch_results: list[dict[str, Any]] = field(default_factory=list)
    iteration_config_paths: list[str] = field(default_factory=list)
    final_summary: dict[str, Any] | None = None
    memory_record: dict[str, Any] | None = None
    memory_summary: dict[str, Any] | None = None


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def load_yaml(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle)
    if not isinstance(data, dict):
        raise ValueError(f"Expected YAML mapping in {path}")
    return data


def load_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict):
        raise ValueError(f"Expected JSON mapping in {path}")
    return data


def write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def default_run_id(config: dict[str, Any]) -> str:
    scenario_id = str(config.get("scenario_id", "scenario"))
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    safe_scenario_id = "".join(ch if ch.isalnum() or ch in ("-", "_") else "_" for ch in scenario_id)
    return f"{safe_scenario_id}_{timestamp}"


def scenario_id(config: dict[str, Any]) -> str:
    value = config.get("scenario_id")
    if not value:
        raise ValueError("Scenario config must include scenario_id")
    return str(value)


def solver_hints(config: dict[str, Any]) -> SolverHints:
    settings = config.get("solver_settings", {})
    if not isinstance(settings, dict):
        settings = {}
    return SolverHints(
        mip_gap=settings.get("relative_mip_gap"),
        time_limit_s=settings.get("time_limit_seconds"),
        warm_start=False,
    )


def controller_decide(
    run_id: str,
    iteration_state: IterationState,
    hints: SolverHints,
    downstream_status: dict[str, Any] | None = None,
) -> ControllerDecision:
    remaining_iterations = max(iteration_state.max_iterations - iteration_state.current_iteration, 0)
    remaining_wall_time = max(
        iteration_state.wall_time_budget_s - iteration_state.elapsed_wall_time_s,
        0.0,
    )
    downstream_status = downstream_status or {}

    if iteration_state.current_iteration >= iteration_state.max_iterations:
        return decision(
            run_id,
            iteration_state,
            ACTION_TERMINATE_FAILURE,
            "ITERATION_BUDGET_EXHAUSTED",
            None,
            hints,
            remaining_iterations,
            remaining_wall_time,
            True,
        )

    if iteration_state.elapsed_wall_time_s >= iteration_state.wall_time_budget_s * 0.85:
        return decision(
            run_id,
            iteration_state,
            ACTION_TERMINATE_FAILURE,
            "WALL_TIME_BUDGET_EXHAUSTED",
            None,
            hints,
            remaining_iterations,
            remaining_wall_time,
            True,
        )

    if downstream_status.get("solver_log_analysis") in {"INFEASIBLE", "NUMERICAL_ERROR"}:
        return decision(
            run_id,
            iteration_state,
            ACTION_TERMINATE_FAILURE,
            f"Downstream solver status is {downstream_status['solver_log_analysis']}",
            None,
            hints,
            remaining_iterations,
            remaining_wall_time,
            True,
        )

    return decision(
        run_id,
        iteration_state,
        ACTION_LAUNCH_SOLVER,
        "Launching solver for the current scenario configuration.",
        "model_executor",
        hints,
        remaining_iterations,
        remaining_wall_time,
        False,
    )


def validation_decision(run_id: str, iteration_state: IterationState, hints: SolverHints) -> ControllerDecision:
    remaining_iterations = max(iteration_state.max_iterations - iteration_state.current_iteration, 0)
    remaining_wall_time = max(
        iteration_state.wall_time_budget_s - iteration_state.elapsed_wall_time_s,
        0.0,
    )
    return decision(
        run_id,
        iteration_state,
        ACTION_VALIDATE_SCENARIO,
        "Validating scenario inputs before model execution.",
        "scenario_validator",
        hints,
        remaining_iterations,
        remaining_wall_time,
        False,
    )


def output_analysis_decision(
    run_id: str,
    iteration_state: IterationState,
    hints: SolverHints,
) -> ControllerDecision:
    remaining_iterations = max(iteration_state.max_iterations - iteration_state.current_iteration, 0)
    remaining_wall_time = max(
        iteration_state.wall_time_budget_s - iteration_state.elapsed_wall_time_s,
        0.0,
    )
    return decision(
        run_id,
        iteration_state,
        ACTION_ANALYZE_OUTPUTS,
        "Analyzing solver outputs for physical and economic plausibility.",
        "output_analysis_agent",
        hints,
        remaining_iterations,
        remaining_wall_time,
        False,
    )


def solver_log_analysis_decision(
    run_id: str,
    iteration_state: IterationState,
    hints: SolverHints,
) -> ControllerDecision:
    remaining_iterations = max(iteration_state.max_iterations - iteration_state.current_iteration, 0)
    remaining_wall_time = max(
        iteration_state.wall_time_budget_s - iteration_state.elapsed_wall_time_s,
        0.0,
    )
    return decision(
        run_id,
        iteration_state,
        ACTION_ANALYZE_SOLVER_LOG,
        "Analyzing solver log for infeasibility, timeout, and numerical issues.",
        "solver_log_analyzer",
        hints,
        remaining_iterations,
        remaining_wall_time,
        False,
    )


def refiner_decision(
    run_id: str,
    iteration_state: IterationState,
    hints: SolverHints,
) -> ControllerDecision:
    remaining_iterations = max(iteration_state.max_iterations - iteration_state.current_iteration, 0)
    remaining_wall_time = max(
        iteration_state.wall_time_budget_s - iteration_state.elapsed_wall_time_s,
        0.0,
    )
    return decision(
        run_id,
        iteration_state,
        ACTION_TRIGGER_REFINER,
        "Generating targeted refinement recommendations from diagnostics.",
        "refiner_agent",
        hints,
        remaining_iterations,
        remaining_wall_time,
        False,
    )


def decision(
    run_id: str,
    iteration_state: IterationState,
    action: str,
    reason: str,
    next_skill: str | None,
    hints: SolverHints,
    remaining_iterations: int,
    remaining_wall_time: float,
    termination_met: bool,
) -> ControllerDecision:
    if action == ACTION_VALIDATE_SCENARIO:
        event = "VALIDATION_START"
    elif action == ACTION_TRIGGER_REFINER:
        event = "REFINER_START"
    elif action == ACTION_ANALYZE_SOLVER_LOG:
        event = "SOLVER_LOG_ANALYSIS_START"
    elif action == ACTION_ANALYZE_OUTPUTS:
        event = "OUTPUT_ANALYSIS_START"
    elif action == ACTION_LAUNCH_SOLVER:
        event = "ITERATION_START"
    else:
        event = "TERMINATION"
    budget_remaining = remaining_iterations
    if action == ACTION_LAUNCH_SOLVER:
        budget_remaining = max(remaining_iterations - 1, 0)
    display_iteration = iteration_state.current_iteration + 1
    if action in {ACTION_TRIGGER_REFINER, ACTION_ANALYZE_SOLVER_LOG, ACTION_ANALYZE_OUTPUTS}:
        display_iteration = max(iteration_state.current_iteration, 1)
    return ControllerDecision(
        run_id=run_id,
        iteration=display_iteration,
        action=action,
        reason=reason,
        next_skill=next_skill,
        updated_solver_hints=hints,
        iteration_budget_remaining=budget_remaining,
        wall_time_remaining_s=remaining_wall_time,
        termination_criteria_met=termination_met,
        log_entry={
            "timestamp": utc_now(),
            "event": event,
            "details": reason,
        },
    )


def terminal_decision(
    run_id: str,
    iteration_state: IterationState,
    action: str,
    reason: str,
    hints: SolverHints,
) -> ControllerDecision:
    remaining_iterations = max(iteration_state.max_iterations - iteration_state.current_iteration, 0)
    remaining_wall_time = max(
        iteration_state.wall_time_budget_s - iteration_state.elapsed_wall_time_s,
        0.0,
    )
    final_iteration = max(iteration_state.current_iteration, 1)
    return ControllerDecision(
        run_id=run_id,
        iteration=final_iteration,
        action=action,
        reason=reason,
        next_skill=None,
        updated_solver_hints=hints,
        iteration_budget_remaining=remaining_iterations,
        wall_time_remaining_s=remaining_wall_time,
        termination_criteria_met=True,
        log_entry={
            "timestamp": utc_now(),
            "event": "TERMINATION",
            "details": reason,
        },
    )


def resolve_config_output_path(repo: Path, config: dict[str, Any], output_key: str) -> Path | None:
    output_files = config.get("expected_output_files", {})
    if not isinstance(output_files, dict):
        return None
    spec = output_files.get(output_key)
    if not isinstance(spec, dict) or not spec.get("path"):
        return None
    return (repo / str(spec["path"])).resolve()


def execute_model(
    command: list[str],
    cwd: Path,
    run_dir: Path,
    config: dict[str, Any],
) -> ExecutorResult:
    stdout_log = run_dir / "model_stdout.log"
    stderr_log = run_dir / "model_stderr.log"
    start = time.perf_counter()
    try:
        completed = subprocess.run(
            command,
            cwd=cwd,
            text=True,
            capture_output=True,
            check=False,
        )
        elapsed = time.perf_counter() - start
        stdout_log.write_text(completed.stdout, encoding="utf-8")
        stderr_log.write_text(completed.stderr, encoding="utf-8")
    except OSError as exc:
        elapsed = time.perf_counter() - start
        stdout_log.write_text("", encoding="utf-8")
        stderr_log.write_text(str(exc), encoding="utf-8")
        return ExecutorResult(
            status="CRASHED",
            command=command,
            return_code=1,
            stdout_log=str(stdout_log),
            stderr_log=str(stderr_log),
            elapsed_s=elapsed,
            error=str(exc),
        )

    summary_path = resolve_config_output_path(cwd, config, "solve_summary")
    summary = None
    if summary_path and summary_path.exists():
        summary = load_json(summary_path)

    status = "COMPLETED" if completed.returncode == 0 else "FAILED"
    if summary and str(summary.get("termination_status", "")).upper() not in {"OPTIMAL", "FEASIBLE_POINT"}:
        status = "FAILED"

    return ExecutorResult(
        status=status,
        command=command,
        return_code=completed.returncode,
        stdout_log=str(stdout_log),
        stderr_log=str(stderr_log),
        elapsed_s=elapsed,
        solve_summary_path=str(summary_path) if summary_path else None,
        solve_summary=summary,
        error=None if completed.returncode == 0 else f"Model command returned {completed.returncode}",
    )


def create_run_state(
    run_id: str,
    config_path: Path,
    config: dict[str, Any],
    run_dir: Path,
    max_iterations: int,
    wall_time_budget_s: float,
) -> RunState:
    return RunState(
        run_id=run_id,
        scenario_id=scenario_id(config),
        scenario_config_path=str(config_path),
        scenario_config_hash=file_sha256(config_path),
        run_dir=str(run_dir),
        created_at=utc_now(),
        iteration_state=IterationState(
            current_iteration=0,
            max_iterations=max_iterations,
            wall_time_budget_s=wall_time_budget_s,
        ),
    )


def persist_final_state(state: RunState, run_dir: Path, history_path: Path) -> None:
    state_dict = asdict(state)
    memory_record = append_history_record(history_path, state_dict)
    memory_summary = summarize_history(history_path)
    state.memory_record = memory_record
    state.memory_summary = memory_summary
    write_json(run_dir / "run_state.json", asdict(state))


def trigger_refiner_for_state(
    state: RunState,
    run_id: str,
    config: dict[str, Any],
    validation_result: dict[str, Any] | None = None,
    solver_log_result: dict[str, Any] | None = None,
    output_analysis_result: dict[str, Any] | None = None,
    executor_result: dict[str, Any] | None = None,
):
    refinement_decision = refiner_decision(run_id, state.iteration_state, solver_hints(config))
    state.controller_decisions.append(asdict(refinement_decision))
    refinement_result = refine_run(
        run_id=run_id,
        iteration=max(state.iteration_state.current_iteration, 1),
        config=config,
        validation_result=validation_result,
        solver_log_result=solver_log_result,
        output_analysis_result=output_analysis_result,
        executor_result=executor_result,
    )
    state.refinement_results.append(refinement_result.to_dict())
    return refinement_result


def maybe_apply_safe_rerun_patch(
    args: argparse.Namespace,
    state: RunState,
    run_dir: Path,
    current_config: dict[str, Any],
    refinement_result: dict[str, Any],
) -> bool:
    if state.iteration_state.current_iteration >= state.iteration_state.max_iterations:
        return False
    if refinement_result.get("recommendation") != "CONTINUE":
        return False
    if not args.enable_reruns and state.iteration_state.max_iterations <= 1:
        return False

    next_iteration = state.iteration_state.current_iteration + 1
    patched_config, patch_result = create_safe_patched_config(
        base_config=current_config,
        refinement=refinement_result,
        run_dir=run_dir,
        iteration=next_iteration,
    )
    state.patch_results.append(patch_result.to_dict())
    if patched_config is None or patch_result.status != "APPLIED" or not patch_result.patched_config_path:
        return False
    state.iteration_config_paths.append(patch_result.patched_config_path)
    return True


def run_orchestration(args: argparse.Namespace) -> int:
    root = repo_root()
    config_path = args.config.resolve()
    config = load_yaml(config_path)
    run_id = args.run_id or default_run_id(config)
    run_dir = (args.runs_dir.resolve() / run_id)
    history_path = args.runs_dir.resolve() / "run_history.jsonl"
    run_dir.mkdir(parents=True, exist_ok=True)

    state = create_run_state(
        run_id=run_id,
        config_path=config_path,
        config=config,
        run_dir=run_dir,
        max_iterations=args.max_iterations,
        wall_time_budget_s=args.wall_time_budget_s,
    )
    state.iteration_config_paths.append(str(config_path))
    write_json(run_dir / "run_state.json", asdict(state))

    current_config = config
    current_config_path = config_path
    terminal_message = "Model execution and output analysis completed successfully."

    while True:
        validate_decision = validation_decision(run_id, state.iteration_state, solver_hints(current_config))
        state.controller_decisions.append(asdict(validate_decision))
        validation_result = validate_scenario_config(current_config, root)
        state.validation_results.append(validation_result.to_dict())
        write_json(run_dir / "run_state.json", asdict(state))

        if validation_result.status == "FAIL":
            refinement_result = trigger_refiner_for_state(
                state=state,
                run_id=run_id,
                config=current_config,
                validation_result=validation_result.to_dict(),
            )
            if maybe_apply_safe_rerun_patch(
                args=args,
                state=state,
                run_dir=run_dir,
                current_config=current_config,
                refinement_result=refinement_result.to_dict(),
            ):
                current_config_path = Path(state.iteration_config_paths[-1])
                current_config = load_yaml(current_config_path)
                continue
            state.status = "FAILED"
            terminal_message = "Scenario validation failed."
            break

        decision_obj = controller_decide(run_id, state.iteration_state, solver_hints(current_config))
        state.controller_decisions.append(asdict(decision_obj))
        if decision_obj.action != ACTION_LAUNCH_SOLVER:
            state.status = "FAILED"
            terminal_message = decision_obj.reason
            break

        command = [sys.executable, str(args.executor_script.resolve()), "--config", str(current_config_path)]
        executor_result = execute_model(command, root, run_dir, current_config)
        state.executor_results.append(asdict(executor_result))
        state.iteration_state.current_iteration += 1
        state.iteration_state.elapsed_wall_time_s += executor_result.elapsed_s

        if executor_result.status != "COMPLETED":
            refinement_result = trigger_refiner_for_state(
                state=state,
                run_id=run_id,
                config=current_config,
                executor_result=asdict(executor_result),
            )
            if maybe_apply_safe_rerun_patch(
                args=args,
                state=state,
                run_dir=run_dir,
                current_config=current_config,
                refinement_result=refinement_result.to_dict(),
            ):
                current_config_path = Path(state.iteration_config_paths[-1])
                current_config = load_yaml(current_config_path)
                continue
            state.status = "FAILED"
            terminal_message = executor_result.error or "Model execution failed."
            break

        log_decision = solver_log_analysis_decision(run_id, state.iteration_state, solver_hints(current_config))
        state.controller_decisions.append(asdict(log_decision))
        log_result = analyze_solver_log(current_config, root, asdict(executor_result))
        state.solver_log_analysis_results.append(log_result.to_dict())

        if log_result.overall_status in {"INFEASIBLE", "NUMERICAL_ERROR", "TIMEOUT_SUBOPTIMAL"}:
            refinement_result = trigger_refiner_for_state(
                state=state,
                run_id=run_id,
                config=current_config,
                solver_log_result=log_result.to_dict(),
                executor_result=asdict(executor_result),
            )
            if maybe_apply_safe_rerun_patch(
                args=args,
                state=state,
                run_dir=run_dir,
                current_config=current_config,
                refinement_result=refinement_result.to_dict(),
            ):
                current_config_path = Path(state.iteration_config_paths[-1])
                current_config = load_yaml(current_config_path)
                continue
            state.status = "FAILED"
            terminal_message = f"Solver log analysis reported {log_result.overall_status}."
            break

        analysis_decision = output_analysis_decision(run_id, state.iteration_state, solver_hints(current_config))
        state.controller_decisions.append(asdict(analysis_decision))
        analysis_result = analyze_outputs(current_config, root, executor_result.solve_summary)
        state.output_analysis_results.append(analysis_result.to_dict())

        if log_result.overall_status != "OK" or analysis_result.overall_status != "OK":
            trigger_refiner_for_state(
                state=state,
                run_id=run_id,
                config=current_config,
                solver_log_result=log_result.to_dict(),
                output_analysis_result=analysis_result.to_dict(),
                executor_result=asdict(executor_result),
            )

        if analysis_result.overall_status == "ANOMALY":
            state.status = "FAILED"
            terminal_message = "Output analysis detected anomalies."
            break

        state.status = "COMPLETED"
        state.final_summary = executor_result.solve_summary
        break

    final_action = ACTION_TERMINATE_SUCCESS if state.status == "COMPLETED" else ACTION_TERMINATE_FAILURE
    final_decision = terminal_decision(
        run_id,
        state.iteration_state,
        final_action,
        terminal_message,
        solver_hints(current_config),
    )
    state.controller_decisions.append(asdict(final_decision))

    persist_final_state(state, run_dir, history_path)
    print(f"Run {state.run_id} {state.status}. State: {run_dir / 'run_state.json'}")
    return 0 if state.status == "COMPLETED" else 1


def show_history(args: argparse.Namespace) -> int:
    history_path = args.runs_dir.resolve() / "run_history.jsonl"
    summary = summarize_history(history_path)
    print(json.dumps(summary, indent=2))
    return 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="AI energy modeling orchestration CLI.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    run_parser = subparsers.add_parser("run", help="Run one orchestrated model execution.")
    run_parser.add_argument("--config", type=Path, required=True)
    run_parser.add_argument(
        "--executor-script",
        type=Path,
        default=repo_root() / "test" / "capacity_expansion_test_1" / "run_test_scenario_1.py",
    )
    run_parser.add_argument("--runs-dir", type=Path, default=repo_root() / "runs")
    run_parser.add_argument("--run-id", default=None)
    run_parser.add_argument("--max-iterations", type=int, default=1)
    run_parser.add_argument("--wall-time-budget-s", type=float, default=7200.0)
    run_parser.add_argument(
        "--enable-reruns",
        action="store_true",
        help="Allow safe recommendation-driven reruns. Reruns also activate when --max-iterations is greater than 1.",
    )
    run_parser.set_defaults(func=run_orchestration)

    history_parser = subparsers.add_parser("history", help="Summarize persisted run history.")
    history_parser.add_argument("--runs-dir", type=Path, default=repo_root() / "runs")
    history_parser.set_defaults(func=show_history)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
