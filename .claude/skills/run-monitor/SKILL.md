---
name: run-monitor
description: Watch an optimization run while it solves — tail its solver.log through the framework's live monitor, read the events (progress, incumbent, stall, time-limit-near, infeasible, traceback, warning), and decide whether to keep waiting, stop and apply a Tier-A change, hand off to log-analyzer, or abort. Use when a solve is taking long, when the user asks "is it still running / is it stuck?", or to bound waiting inside the refine loop. Model-agnostic; never edits a policy key mid-run.
---

# Run Monitor — analyze while it runs

`model-runner` launches a solve; this skill watches it. The framework streams
the model's output line by line (`framework.process.stream_command`) through
`framework.monitor.RunMonitor`, which turns solver and wrapper log lines into
typed events and keeps a rolling summary. You read that summary and the events,
and decide. You do not diagnose root causes here (that is `log-analyzer`) and
you never change a config here (that is `refiner`).

## How to watch

1. **From a shell, any time, for any run dir** (what a human and the agent
   both use):
   ```bash
   python -m framework.watch runs/<name>            # one-screen summary + last events
   python -m framework.watch runs/<name> --follow   # keep tailing until the run ends
   python -m framework.watch runs/<name> --json     # the summary as JSON (for scripts / you)
   ```
   It replays `<run_dir>/solver.log` through the monitor, merges the live
   `monitor.json` the adapter writes, shows `run.pid` liveness, and in
   `--follow` mode also runs stall / time-limit checks on wall time
   (`--stall-seconds`, `--time-limit`).

2. **In-process, while running** — pass a listener to the runner:
   ```python
   from framework import get_adapter, run_and_record
   events = []
   record = run_and_record(get_adapter(), config, run_dir,
                           on_event=lambda ev: (events.append(ev), print(ev)))
   record.execution.monitor          # rolling summary (phase, best_obj, bound, last_gap, ...)
   ```
   Adapters that stream get the listener live; for the rest the events are
   replayed from `solver.log` after the run. Either way `execution.monitor`
   is filled and `<run_dir>/monitor.json` holds the last ~200 events.

3. **After the fact** — `RunRecord.load(...).execution.monitor`, or
   `framework.monitor.replay_log(path)` on any saved `solver.log`.

## What the events mean

| kind | meaning | what to do |
|---|---|---|
| `solver_start`, `model_size`, `phase` | solver banner; rows/cols/nonzeros; phase change (presolve → root relaxation / barrier / simplex → branch_and_bound → finished) | nothing; note model size for the report |
| `progress` | a MIP tree row (incumbent, bound, gap, nodes, solver time) | watch the gap trend, not single rows |
| `incumbent` | a new feasible solution was found | good sign: the run is making progress |
| `barrier_iter`, `simplex_iter`, `root_relaxation` | LP progress | nothing unless it repeats for very long |
| `solution`, `explored`, `solver_status` | the solver's final objective / bound / gap and its own verdict (`OPTIMAL`, `INFEASIBLE`, `UNBOUNDED`, `TIME_LIMIT`, `LIMIT`, `ERROR`) | the run is ending; wait for the adapter's `run_record.json` |
| `status` | the model wrapper's marker (`... solved successfully`, `... is infeasible`, `... reached the time limit`, `... did not solve`, `status: <S>`) | same — this is what `termination_status` will say |
| `preflight` | the model's own preflight passed / failed | failed → the run will end as `ERROR`/`preflight`; hand to `log-analyzer` |
| `warning` | a `WARNING` line (solver or model) | keep for `output-analyzer`; a planted anomaly often announces itself here |
| `error`, `traceback` | an `ERROR:` line; a Julia/Python stack trace | the run is crashing (`error_origin: runtime`); wait for it to end, then `log-analyzer` |
| `stall` | no incumbent/bound improvement (or no output at all) for `stall_seconds` (default 600 s) | see below |
| `time_limit_near` | elapsed ≥ 90 % of the configured time limit | decide now whether the incumbent is good enough |

The summary (`monitor.json`, `execution.monitor`) has: `solver`, `is_mip`,
`phase`, `status` (+ `status_source`: solver vs wrapper marker), `best_obj`,
`bound`, `last_gap`/`gap_pct`, `nodes`, `n_incumbents`, `n_warnings` +
`warnings`, `n_errors` + `errors`, `traceback`, `stalled` + `stall_reason`,
`time_limit` + `time_limit_near`, `solver_time_s`, `elapsed_s`, `aborted`.

## When to intervene

Reason from the summary, in this order:

1. **A terminal status has appeared** (`INFEASIBLE`, `UNBOUNDED`, `ERROR`,
   traceback): stop waiting. Let the process exit on its own (the adapter
   still needs to write `run_record.json`), then hand the run to
   `log-analyzer`. Do not abort an infeasible run early — the tail of the
   log is the evidence the analyzer needs.
2. **Progress is healthy** (incumbents keep arriving, gap shrinking, no
   stall): wait. Report the gap trend and the projected time if asked.
3. **`stall`** (gap flat for the stall window) — look at the gap:
   - gap already within a tolerance the study can accept (it is *your*
     judgement to state, the modeler's to accept): abort, then have `refiner`
     apply a **Tier-A** change (raise `mipgap`, or lower `time_limit`) so the
     re-run ends cleanly with an incumbent, or accept the `TIME_LIMIT` result.
   - gap still large and the bound not moving: this is usually a hard
     instance, not a bug. Options are all Tier A (more `time_limit`, a
     different solver / LP method, a looser `mipgap`) — propose them to the
     user; do not touch anything else.
4. **`time_limit_near`**: the adapter will return `TIME_LIMIT` (with the
   incumbent when the model extracts one). Decide whether that incumbent is
   acceptable or whether a re-run with a higher `time_limit` (Tier A) is
   warranted.
5. **Warnings**: never act on them mid-run; pass them to `output-analyzer`
   after an `OPTIMAL` result.

**Guardrail (non-negotiable):** nothing you see while watching justifies
editing a policy key (a cap, floor, share, or scope — Tier C). "It is taking
too long" or "it says infeasible" is not evidence that the policy is wrong.
Stalls and time limits are Tier-A problems; infeasibility is a diagnosis
problem for `log-analyzer`, and any policy relaxation goes through `refiner`,
which blocks Tier C for a human. Aborting a run is a scheduling decision, not
a fix.

## How to abort

- From a shell: `python -m framework.watch runs/<name> --abort "gap 0.6% flat for 20 min"`.
  This writes `<run_dir>/ABORT` (the streaming runner polls it and terminates
  the solver, SIGTERM then SIGKILL after a grace period) and, if the child
  ignores it, signals the pid in `<run_dir>/run.pid`.
- In-process: keep a reference to the `RunMonitor` (or the `threading.Event`
  you passed as `abort`) and call `monitor.request_abort("reason")`.
- After an abort the adapter still returns an `Execution` — typically
  `ERROR`/`runtime` or `TIME_LIMIT`, with `monitor.aborted = true` and the
  reason — and `run_and_record` still writes the record. Say in your report
  that the run was aborted and why; never present an aborted run as a solve.

## Output

One paragraph: solver and phase, elapsed vs. time limit, incumbent / bound /
gap and the trend, warnings and errors seen, whether it is stalled, and the
decision — *waiting*, *hand to log-analyzer*, *abort + Tier-A re-run
(which key, why)*, or *needs the user* — with the evidence event(s) cited.
