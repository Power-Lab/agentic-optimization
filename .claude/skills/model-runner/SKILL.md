---
name: model-runner
description: Execute one optimization config through the active model adapter, capturing the solver log and writing a run_record.json. Use when a config is ready to run, when the user says "run this scenario", or as the execute step in the refine loop. Model-agnostic; never edits the model; tees solver output to solver.log.
---

# Model Runner (Task 2)

Run exactly one config and produce the artifacts the analyzers/refiner read.
This is the only skill that launches the solver.

## Operating rules

1. **Resolve the adapter; use the framework runner.** One call:
   ```python
   from framework import get_adapter, run_and_record
   adapter = get_adapter()
   record = run_and_record(adapter, config, run_dir, parent_run=parent_hash)
   ```
   `run_and_record` runs adapter preflight, executes the model, captures
   `solver.log`, archives outputs into `<run_dir>/outputs/`, and writes
   `<run_dir>/run_record.json`.

2. **A failed solve is a result, not an error.** Infeasible / time-limit /
   preflight-error runs still produce a `run_record.json` with the
   `termination_status` set and `solver.log` populated. Never swallow or retry
   silently — hand the record to `log-analyzer`.

3. **One run dir per attempt** (e.g. `runs/<name>/`, suffix `_iter2` in a loop)
   so the supervisor's ledger stays one-folder-per-run. Set `parent_run` to the
   prior attempt's `config_hash` when this run came from a refinement.

4. **Report**, don't interpret: termination status, wall seconds, the
   `solver.log` path, where outputs landed. Diagnosis is the analyzer's job.

## Environment notes

- The adapter owns *where* execution happens. A local adapter runs the solver on
  this machine; a remote adapter ships it to a node. The skill code is identical
  either way.
- Requires whatever the adapter needs (for the village example: Julia + a Gurobi
  licence, and a one-time `bootstrap.jl` after a fresh checkout).
