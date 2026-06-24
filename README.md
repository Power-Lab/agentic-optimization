# AI Energy System Modeling

This repository contains a research/prototype workspace for AI-assisted energy system modeling. It combines energy optimization examples, modular agent skills, and an executable orchestration layer for validating, running, diagnosing, refining, and tracking optimization runs.

## What Is Included

- `agent/`: executable orchestration layer for the AI modeling agent.
- `skill_library/`: reusable skill documentation and assets for scenario building, execution, solver-log analysis, output analysis, refinement, and iteration memory.
- `.codex/skills/`: local Codex skill definitions used by this workspace.
- `doc/`: notebooks, workflow documentation, data files, and update briefs.
- `test/`: capacity expansion scenario fixtures and solver outputs.
- `literature/`: reference papers for agentic optimization and LLM-assisted modeling.

## Agent Workflow

The current agent workflow is:

```text
VALIDATE_SCENARIO
LAUNCH_SOLVER
ANALYZE_SOLVER_LOG
ANALYZE_OUTPUTS
TRIGGER_REFINER
SAFE_PATCH_AND_RERUN
ITERATION_MEMORY_APPEND
```

The full workflow guide is available at:

```text
doc/Agent Workflow Guide.md
```

## Quick Start

Run the current capacity expansion smoke scenario:

```powershell
python -m agent.orchestrator run --config test\capacity_expansion_test_1\test_scenario_1_output.yaml --run-id orchestration_smoke_test
```

Allow safe recommendation-driven reruns:

```powershell
python -m agent.orchestrator run --config test\capacity_expansion_test_1\test_scenario_1_output.yaml --run-id controlled_rerun_smoke --max-iterations 2
```

View accumulated run memory:

```powershell
python -m agent.orchestrator history
```

Compile-check agent modules:

```powershell
python -m py_compile agent\orchestrator.py agent\validation.py agent\output_analysis.py agent\solver_log_analysis.py agent\refiner.py agent\iteration_memory.py agent\scenario_patching.py
```

## Current Scope

The agent currently supports scenario validation, model execution, solver log analysis, output analysis, recommendation-only refinement, safe solver-setting patches, controlled reruns, and append-only iteration memory.

Structural scenario edits, generator additions, demand changes, and policy relaxations remain recommendation-only until a broader patch approval workflow is added.

