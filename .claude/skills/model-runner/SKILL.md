---
name: model-runner
description: Execute one optimization config through the active model adapter, capturing the solver log and writing a run_record.json. Use when a config is ready to run, when the user says "run this scenario", or as the execute step in the refine loop. Never edits the model; tees solver stdout to solver.log.
---

# Model Runner (Task 2)

Run exactly one config and produce the artifacts the analyzer/refiner read.
This is the only skill that launches the solver.

## Operating rules

1. **Use the framework, do not shell out ad hoc.** The one call is:
   ```python
   from adapters.village import VillageAdapter
   from framework import run_and_record
   record = run_and_record(VillageAdapter(), config, run_dir, parent_run=parent_hash)
   ```
   `run_and_record` runs adapter preflight, executes the model, captures
   `solver.log`, archives the output CSVs into `<run_dir>/outputs/`, and writes
   `<run_dir>/run_record.json`.

2. **A failed solve is a result, not an error.** Infeasible / time-limit /
   preflight-error runs still produce a `run_record.json` with the
   `termination_status` set and `solver.log` populated. Never swallow or retry
   silently — hand the record to `log-analyzer`.

3. **One run dir per attempt.** Name it for the config (e.g.
   `runs/<scenario>_<island>_<year>_<clean>/`, suffix `_iter2` etc. in a loop)
   so the supervisor's ledger stays one-folder-per-run. Set `parent_run` to the
   prior attempt's `config_hash` when this run came from a refinement.

4. **Report**, don't interpret: termination status, wall seconds, the
   `solver.log` path, and where the output CSVs landed. Diagnosis is the
   analyzer's job.

## Environment notes

- First run after a fresh model checkout needs a one-time
  `julia --project=. bootstrap.jl` in the model root (instantiates packages,
  validates Gurobi).
- Requires a working Gurobi licence (`~/gurobi.lic` or `GRB_LICENSE_FILE`).
- The Maluku reference case solves in a few minutes and has a committed
  baseline — use it to confirm the environment before a big run.
