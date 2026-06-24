# Skill Library README

## Purpose

This repository contains a collection of modular agent skills for AI-driven energy system modeling. Each skill encapsulates a specific capability in an iterative optimization workflow, from scenario generation and solver orchestration to log analysis, output validation, refinement, and memory tracking.

## High-level workflow

1. code_generator generates, edits, refactors, or repairs the code, scripts, configs, and solver/HPC glue used by the workflow.
2. scenario_builder creates a solver-ready scenario config from high-level requirements.
3. supervisory_controller orchestrates the iterative loop, decides when to run the solver, stop, or invoke refinement.
4. model_executor runs the optimization model on the scenario config.
5. solver_log_analyzer inspects solver logs for infeasibility, numerical issues, or other failure modes.
6. output_analysis_agent validates and analyzes solver outputs for anomalies or violations.
7. refiner_agent proposes fixes, adjustments, or structural changes when the solver or outputs are problematic.
8. iteration_memory records the history of runs, convergence trends, failure patterns, and best solutions.
9. The loop repeats until termination criteria are met.

## Skill folders

- code_generator
  - Generates, edits, refactors, and debugs Python, Julia/JuMP, Gurobi, sbatch, YAML, and JSON code for energy system modeling workflows.
  - Supports orchestration scripts, model code, solver configuration, HPC submission, and structured config generation.

- iteration_memory
  - Tracks past optimization runs, convergence trends, and failure patterns.
  - Supports queries for comparing runs, avoiding repeated failures, and identifying best solutions.

- model_executor
  - Executes the energy system model or solver using a validated scenario config.
  - Serves as the core runtime component that produces dispatch results and solver logs.

- output_analysis_agent
  - Inspects solver outputs for physical or model anomalies.
  - Flags issues like power imbalance, constraint violations, or unexpected solution characteristics.

- refiner_agent
  - Generates targeted fixes or parameter adjustments based on solver and output issues.
  - Can recommend stopping, retrying with revised parameters, or rebuilding the scenario.

- scenario_builder
  - Converts abstract energy system goals into detailed, solver-ready configuration files.
  - Handles initial scenario construction and re-builds after refinement requests.

- solver_log_analyzer
  - Parses and interprets solver logs for infeasibility, numerical instability, timeouts, and convergence behavior.
  - Produces structured diagnostics for the supervisory controller and refiner.

- supervisory_controller
  - Orchestrates session state, iteration budgeting, and decision routing across the skill chain.
  - Determines whether to continue, stop, or trigger downstream skills.

## Standard skill structure

Each skill folder follows a consistent layout:

- SKILL.md
  - Describes the skill name, purpose, expected inputs, outputs, and invocation guidance.
  - Serves as the primary documentation for the skill's role in the workflow.

- `scripts/`
  - Contains implementation utilities, validation scripts, or example executables for the skill.
  - Often includes helpers used by the agent or for local testing.

- `assets/`
  - Stores sample inputs, outputs, logs, or config files that illustrate the skill’s behavior.
  - Useful for quick reference, examples, or regression cases.

- `references/`
  - Holds domain notes, modeling assumptions, solver heuristics, or analysis guidance.
  - Provides contextual knowledge used by the skill or by developers understanding the workflow.

## Usage note

This repository is intended as a skill library rather than a stand-alone application. Developers and researchers can use the folder structure as a guide for adding new skills, understanding interaction patterns, and reusing common documentation conventions.
