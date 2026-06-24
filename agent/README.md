# Agent Orchestration Layer

This package is the first executable layer for the AI energy modeling agent. It wraps an existing model runner with:

- a stable run-state file
- controller decisions
- scenario validation diagnostics
- subprocess execution logs
- solver summary collection
- basic budget checks

For full workflow documentation, code structure, and examples, see:

```text
doc\Agent Workflow Guide.md
```

## Smoke Run

From the repository root:

```powershell
python -m agent.orchestrator run --config test\capacity_expansion_test_1\test_scenario_1_output.yaml --run-id orchestration_smoke_test
```

Allow safe recommendation-driven reruns by increasing the iteration budget:

```powershell
python -m agent.orchestrator run --config test\capacity_expansion_test_1\test_scenario_1_output.yaml --run-id rerun_smoke --max-iterations 2
```

The run state is written to:

```text
runs\orchestration_smoke_test\run_state.json
```

View accumulated run memory:

```powershell
python -m agent.orchestrator history
```

## Current Scope

The orchestration layer currently validates scenario configs, runs model executions through the existing capacity expansion Python runner, analyzes solver logs and outputs, emits refinements, applies safe solver-setting patches when reruns are allowed, and appends compact iteration memory. Structural changes and policy relaxations remain recommendation-only.

## Run-State Contract

`run_state.json` now includes:

- `controller_decisions`: `VALIDATE_SCENARIO`, `LAUNCH_SOLVER`, and terminal decisions.
- `validation_results`: pass/fail status, validation checks, resolved input files, and matched candidate generators.
- `executor_results`: subprocess command, logs, return code, elapsed time, and solve summary.
- `solver_log_analysis_results`: solver-side status, infeasibility/numerical diagnostics, and parsed performance metrics.
- `output_analysis_results`: physical/economic plausibility status, anomalies or warnings, and computed metrics.
- `refinement_results`: recommendation-only fixes or review actions triggered by validation, solver-log, executor, or output diagnostics.
- `patch_results`: safe config patch attempts, including applied and skipped overrides.
- `iteration_config_paths`: config path used by each iteration, including generated rerun configs.
- `final_summary`: the solver summary from the completed run.
- `memory_record`: compact history entry appended to `runs\run_history.jsonl`.
- `memory_summary`: best run, convergence trend, repeated causes, and known failure patterns from persisted history.

## Controlled Rerun Policy

Reruns only occur when the iteration budget allows it and the refiner returns `recommendation = CONTINUE` with safe overrides. The current safe patch boundary is deliberately narrow:

- allowed: `solver_settings.time_limit_seconds`, `solver_settings.relative_mip_gap`, `solver_settings.presolve`, `solver_settings.log_to_console`, and `solver_settings.log_file`
- skipped: unsupported solver hints such as `warm_start`, `NumericFocus`, and `ScaleFlag`
- blocked: policy constraints, structural changes, generator changes, demand changes, and model reformulations

Generated rerun configs are written under `runs\<run_id>\iteration_<n>\scenario_config.yaml`, and output paths are redirected into that iteration folder.
