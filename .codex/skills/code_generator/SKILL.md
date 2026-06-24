---
name: code_generator
description: "Use this skill when asked to generate, edit, refactor, debug, or repair Python, Julia/JuMP, Gurobi, sbatch, YAML, or JSON code for AI-driven energy system modeling workflows, especially for trigger phrases such as write code, fix script, refactor model, add constraint, debug JuMP, configure Gurobi, create sbatch, automate parameter sweep, generate config, or fix errors from logs."
---

# Code Generator

## Purpose

Generate, edit, refactor, and debug production-style but MVP-friendly code used in AI-driven energy system modeling workflows. This skill supports orchestration scripts, optimization model code, solver configuration, HPC submission, structured configuration files, and log-driven fixes.

## When to Use

Use this skill for requests involving:
- Python orchestration scripts for scenario generation, model execution, log parsing, output checks, or parameter sweeps
- Julia / JuMP optimization model code for dispatch, unit commitment, storage, reserves, emissions, or capacity constraints
- Gurobi parameter configuration, time limits, MIP gaps, infeasibility diagnostics, and numerical stability settings
- HPC batch scripts using `sbatch`, module loading, environment activation, job arrays, and reproducible logs
- YAML or JSON config generation and schema-compatible edits
- Model debugging, constraint generation, parameter changes, and incremental model modification
- Log-driven code fixes from solver logs, Python tracebacks, Julia stack traces, or failed batch output

Do not use this skill for purely conceptual modeling discussion unless the user wants code, scripts, configuration, or concrete implementation changes.

## Inputs

Gather only the inputs needed for the requested change:
- Target files, scripts, or config paths
- Scenario assumptions, time horizon, regions, technologies, fuels, emissions limits, and reserve requirements
- Solver details such as Gurobi version, license context, time limit, MIP gap, threads, and compute environment
- Runtime environment: local machine, conda/venv, Julia project, HPC modules, SLURM partition, wall time, and memory
- Error evidence: solver log excerpts, stack traces, failed command, output artifacts, and prior config
- Expected output schema, file naming convention, and downstream skill or tool that consumes the result

## Outputs

Return concrete artifacts, not only advice:
- Edited or generated code files with scoped changes
- YAML/JSON configs with stable keys and explicit units
- `sbatch` scripts with reproducible logs and environment setup
- Structured diagnostics explaining the root cause and fix
- Validation commands or lightweight smoke tests
- Notes about assumptions, remaining risks, and any tests that could not be run

## Workflow Instructions

1. Inspect existing files before editing. Preserve project architecture, naming conventions, data contracts, and solver/output schemas.
2. Make the smallest change that solves the task. Avoid broad rewrites unless the existing code cannot support the requested behavior.
3. Separate concerns: config loading, validation, model construction, solve execution, output writing, and diagnostics should be distinct functions or modules when practical.
4. Prefer deterministic interfaces: `argparse` for CLIs, `pathlib.Path` for paths, `subprocess.run(..., check=True)` for external commands, and `yaml.safe_load` / `json.load` for structured config.
5. Emit structured outputs for downstream agents. Use JSON or YAML for diagnostics, summaries, run metadata, and machine-readable results.
6. For JuMP code, keep sets, parameters, variables, constraints, objective, solve call, and output extraction clearly separated.
7. For Gurobi, set parameters explicitly from config when possible; include `TimeLimit`, `MIPGap`, `Threads`, and diagnostic parameters only when justified.
8. For HPC scripts, include strict shell mode, clear log paths, environment activation, job resources, and command echoing.
9. For debugging, start from the error evidence. Fix the failing boundary first, then add a small guard, validation check, or targeted test to prevent recurrence.
10. Before finishing, verify syntax or run a smoke test when the environment supports it. If verification is blocked, state exactly what was not run and why.

## Heuristics

- Keep code modular without over-engineering. MVP-friendly means simple execution paths, clear errors, and easy extension.
- Preserve existing architecture. Match local style before introducing new helpers or dependencies.
- Use explicit units in variable names or config comments when values represent MW, MWh, dollars, tons CO2, hours, or percentages.
- Validate configs early and fail with actionable messages before launching expensive solver jobs.
- Avoid hidden global state in orchestration scripts. Pass config and paths through function arguments.
- Prefer data-frame or table-driven generation for repeated technologies, regions, and time periods.
- Use named constraints in JuMP so infeasibility and IIS diagnostics are traceable.
- Keep solver tuning conservative. Explain changes to tolerances, presolve, numeric focus, or MIP gap.
- Do not swallow subprocess output. Capture logs to files and surface the command, return code, and relevant stderr.
- When making log-driven fixes, connect each code change to a specific error line or solver symptom.

## Failure Modes

Watch for:
- Config keys that do not match model expectations
- Unit mismatches between demand, generation, storage capacity, and emissions rates
- Off-by-one errors in time indexing or wraparound constraints
- Infeasible reserve, emissions, ramping, or minimum generation assumptions
- Missing solver license or unavailable Gurobi environment on HPC nodes
- Batch scripts that work interactively but fail because modules, paths, or environments are not loaded
- Excessive rewrites that break downstream agents or stored iteration history
- JSON/YAML outputs that are human-readable but not machine-parseable
- Log parsers that depend on brittle line positions instead of robust patterns

## Example Tasks

- "Write a Python runner that reads scenario_config.yaml, launches the Julia model, and writes run_summary.json."
- "Add a CO2 cap constraint to the JuMP dispatch model."
- "Refactor this script to use pathlib, argparse, and structured YAML loading."
- "Create an sbatch script for running a 50-case parameter sweep on SLURM."
- "Fix this Gurobi timeout by adding config-driven solver parameters and better log capture."
- "Generate YAML configs for demand growth and renewable penetration sweeps."
- "Use the solver log to identify the failed constraint family and patch the model code."

## References

Load these only when relevant:
- `references/jump_patterns.md` for Julia / JuMP model structure, constraints, and Gurobi integration.
- `references/python_workflow_patterns.md` for Python CLIs, config handling, subprocess execution, and structured outputs.
- `references/hpc_submission_patterns.md` for SLURM `sbatch` scripts, arrays, logging, and environment setup.

