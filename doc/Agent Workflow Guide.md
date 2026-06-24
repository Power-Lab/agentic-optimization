# AI Energy Modeling Agent Workflow Guide

This guide documents the current executable agent workflow, code structure, run-state contract, and example usage for the `agent/` orchestration layer.

## Current Purpose

The agent is an MVP orchestration layer for AI-assisted energy system modeling. It wraps an existing capacity expansion model runner with deterministic workflow stages:

1. Validate scenario inputs.
2. Launch the model executor.
3. Analyze solver logs.
4. Analyze solver outputs.
5. Trigger refiner recommendations when diagnostics are non-OK.
6. Optionally apply safe solver-setting patches and rerun.
7. Append compact iteration memory.

The current backend is the existing thermal greenfield capacity expansion runner at:

```text
test/capacity_expansion_test_1/run_test_scenario_1.py
```

Structural scenario changes, generator additions, demand changes, and policy relaxations are still recommendation-only.

## Workflow

The main orchestration sequence is:

```text
VALIDATE_SCENARIO
LAUNCH_SOLVER
ANALYZE_SOLVER_LOG
ANALYZE_OUTPUTS
TRIGGER_REFINER        only when diagnostics are WARN/ANOMALY/failure
SAFE_PATCH_AND_RERUN   only when allowed and patch is safe
TERMINATE_SUCCESS or TERMINATE_FAILURE
ITERATION_MEMORY_APPEND
```

### Stage Details

`VALIDATE_SCENARIO`

Checks that the scenario config has required identifiers, input files, demand horizon, candidate generator filters, solver settings, and declared output paths. Validation supports the two current scenario YAML shapes used in the test folders.

`LAUNCH_SOLVER`

Runs the configured model backend as a subprocess. It captures command, return code, stdout log, stderr log, elapsed time, and solve summary.

`ANALYZE_SOLVER_LOG`

Uses `solver_settings.log_file` when available. If the declared solver log is missing, it falls back to captured stdout/stderr and the solve summary. It classifies solver diagnostics as `OK`, `WARN`, `INFEASIBLE`, `NUMERICAL_ERROR`, or `TIMEOUT_SUBOPTIMAL`.

`ANALYZE_OUTPUTS`

Checks solution credibility after a completed solve. Current checks include objective cost consistency, output file existence, nonnegative capacity, hourly power balance, generation capacity limits, and non-served energy severity.

`TRIGGER_REFINER`

Maps diagnostics into recommendation-only next actions. Examples:

- `LOAD_SHEDDING_PRESENT` -> `STOP / NO_FIX`
- timeout -> solver time-limit override
- demand-supply infeasibility -> `RESTRUCTURE`
- missing input file -> `CONFIG_REPAIR`

`SAFE_PATCH_AND_RERUN`

When `--max-iterations` is greater than 1, the orchestrator may apply safe refiner patches and rerun. Current safe patches are limited to selected `solver_settings` keys.

`ITERATION_MEMORY_APPEND`

Every terminal run appends a compact JSON object to:

```text
runs/run_history.jsonl
```

The run state also embeds `memory_record` and `memory_summary`.

## Code Structure

| File | Responsibility |
|---|---|
| `agent/orchestrator.py` | CLI entrypoint, controller decisions, model subprocess execution, iteration loop, terminal state persistence. |
| `agent/validation.py` | Pre-solve scenario validation and path/schema normalization. |
| `agent/solver_log_analysis.py` | Solver log parser and fallback diagnostics from stdout/stderr and solve summary. |
| `agent/output_analysis.py` | Post-solve physical and economic plausibility checks. |
| `agent/refiner.py` | Recommendation-only diagnostic-to-fix mapping. |
| `agent/scenario_patching.py` | Safe scenario patch creation for controlled reruns. |
| `agent/iteration_memory.py` | Append-only JSONL history and summary functions. |
| `agent/README.md` | Quick-start usage and run-state field summary. |

## Run-State Contract

Each run writes:

```text
runs/<run_id>/run_state.json
```

Main fields:

| Field | Description |
|---|---|
| `run_id` | User-supplied or generated run identifier. |
| `scenario_id` | Scenario identifier from the YAML config. |
| `scenario_config_path` | Initial scenario config path. |
| `scenario_config_hash` | SHA-256 hash of the initial scenario config file. |
| `iteration_state` | Current iteration, max iterations, elapsed wall time, and restructure count. |
| `controller_decisions` | Ordered controller actions and routing decisions. |
| `validation_results` | Scenario validation status and diagnostics. |
| `executor_results` | Subprocess command, logs, return code, elapsed time, and solve summary. |
| `solver_log_analysis_results` | Solver-side diagnostics and parsed performance metrics. |
| `output_analysis_results` | Physical/economic output checks and computed metrics. |
| `refinement_results` | Refiner recommendations and source issues. |
| `patch_results` | Safe patch attempts, applied overrides, skipped overrides, and generated config path. |
| `iteration_config_paths` | Config path used by each iteration. |
| `final_summary` | Final accepted solve summary. |
| `memory_record` | Compact record appended to `runs/run_history.jsonl`. |
| `memory_summary` | Best run, convergence trend, repeated causes, and known failure patterns. |

## Controlled Rerun Policy

Reruns are intentionally conservative.

They require:

- `--max-iterations` greater than 1, or `--enable-reruns`
- refiner `recommendation = CONTINUE`
- supported safe overrides
- remaining iteration budget

Currently allowed overrides:

```text
solver_settings.time_limit_seconds
solver_settings.relative_mip_gap
solver_settings.presolve
solver_settings.log_to_console
solver_settings.log_file
```

Currently skipped or blocked:

```text
warm_start
NumericFocus
ScaleFlag
policy_constraints
generator changes
demand changes
structural changes
model reformulations
```

Generated rerun configs are written under:

```text
runs/<run_id>/iteration_<n>/scenario_config.yaml
```

Output paths inside the generated config are redirected into:

```text
runs/<run_id>/iteration_<n>/outputs/
```

## Example Usage

Run the current smoke scenario:

```powershell
python -m agent.orchestrator run --config test\capacity_expansion_test_1\test_scenario_1_output.yaml --run-id orchestration_smoke_test
```

Run with a larger iteration budget:

```powershell
python -m agent.orchestrator run --config test\capacity_expansion_test_1\test_scenario_1_output.yaml --run-id controlled_rerun_smoke --max-iterations 2
```

Explicitly allow safe reruns:

```powershell
python -m agent.orchestrator run --config test\capacity_expansion_test_1\test_scenario_1_output.yaml --run-id safe_rerun_test --max-iterations 2 --enable-reruns
```

Summarize persistent history:

```powershell
python -m agent.orchestrator history
```

Compile-check agent modules:

```powershell
python -m py_compile agent\orchestrator.py agent\validation.py agent\output_analysis.py agent\solver_log_analysis.py agent\refiner.py agent\iteration_memory.py agent\scenario_patching.py
```

## Current Smoke Result

The latest validated smoke scenarios use:

```text
test/capacity_expansion_test_1/test_scenario_1_output.yaml
```

Observed result:

| Metric | Value |
|---|---:|
| Final status | `COMPLETED` |
| Solver status | `OPTIMAL` |
| Objective | `990,873,978.80 USD` |
| Max power-balance imbalance | `0.0 MWh` |
| Max capacity violation | `0.0 MWh` |
| Total non-served energy | `637.0 MWh` |
| NSE fraction of demand | `0.0028%` |
| Refiner recommendation | `STOP / NO_FIX` |

Because the refiner recommends `STOP`, increasing `--max-iterations` does not trigger a rerun for the current scenario. That is expected behavior.

## Development Notes

- The current Python capacity expansion backend does not write the configured HiGHS log path, so solver log analysis falls back to captured stdout/stderr for that backend.
- The Julia test case in `test/capacity_expansion_test_2` includes a real HiGHS log, and the parser can extract model size, simplex iterations, runtime, and objective from it.
- The current history file is append-only. If test runs become noisy, archive or delete `runs/run_history.jsonl` before a fresh demonstration run.
- Before expanding policy or structural patching, add focused tests for validation, output analysis, solver log analysis, refiner rules, scenario patching, and iteration memory.
