# CLAUDE.md — playbook for an agent session in this repo

This file is read by Claude Code sessions working here. It is the operational
companion to [README.md](README.md) (what the repo is),
[docs/USER_GUIDE.md](docs/USER_GUIDE.md) (how a modeler uses it) and
[docs/EXPERIMENTAL_PROTOCOL.md](docs/EXPERIMENTAL_PROTOCOL.md) (the study the
repo exists to run).

## What this repo is

A model-agnostic framework that drives optimization models through a closed
loop — build a scenario, run it, watch the solve, diagnose the log, inspect the
outputs, refine, repeat — with a **scientific-integrity guardrail**: a change
that relaxes a policy constraint is never applied silently. Three models plug
into that one contract. The framework is the artifact; the models are case
studies.

## Hard rules

1. **Never edit anything under `models/`.** `models/garuda` and
   `models/pathways` are git submodules of other people's models. Read them
   freely; changing them (or `.gitmodules`, or a submodule pin) corrupts the
   experiment's provenance.
2. **`framework/` and `.claude/skills/` stay model-agnostic.** No model name,
   filename, or config key may appear there. The check is
   `grep -rn -i 'village\|garuda\|pathways\|pypsa' framework/ .claude/skills/`
   — it must return nothing. Model specifics belong in `adapters/<name>/` and
   `examples/<name>/`.
3. **Always use absolute paths in shell commands.** The working directory
   persists between calls; a stray `cd` into a submodule has contaminated it
   before. Prefer `git -C <abs>` and `cd /abs/path && …`.
4. **Never hand-edit an `expected.json`** under `examples/*/benchmark/` or
   `examples/*/fixtures/` to make a metric look better. Those labels are the
   ground truth; `python -m eval list-tasks` flags an inconsistent one.
5. **A solve costs real time and, for two of the three models, a licence.**
   Check the cost table below before running anything, and prefer the
   `pypsa_toy` model or a garuda dispatch case when you only need *a* run.

## The three models

| Adapter | How it runs | Needs | Results | Typical cost |
|---|---|---|---|---|
| `garuda` | `julia --project=models/garuda run_model.jl --config <cfg>` in the model root | Julia; HiGHS (bundled) or Gurobi | model's `results/<scenario>_<island>_<year>_<clean>[__<run_tag>]/`, archived into `<run_dir>/outputs/` | dispatch LP seconds–minutes; expansion MILP ≈ 25 min on HiGHS (timor_demo), ≈ 5 s on Gurobi |
| `pathways` | `adapters/pathways/driver.py` under `$PATHWAYS_PYTHON`, in a per-run symlink workspace | Gurobi **only**, plus `$PATHWAYS_DATA_ROOT` (Zenodo `data_pkl/`, `data_mat/`, `data_shp/`) | `<run_dir>/outputs/` (year stripped from the names) | minutes (5 days) to ~20 h (full year) |
| `pypsa_toy` | `adapters/pypsa_toy/runner.py` under `$PYPSA_PYTHON`, cwd = repo root | pypsa + linopy + highspy; no licence, no data | `<run_dir>/outputs/*.csv` (always the same six) + `<run_dir>/highs.log` | 1-day LP well under a second; 14-day LP 1–3 s |

Environment variables: `AGENTIC_ADAPTER` (which adapter `get_adapter()` returns
— **required, since three are registered**), `GARUDA_PYTHON` (the interpreter
garuda's own schema validator runs under; the adapter points it at miniforge so
pandas is present), `GARUDA_SKIP_VALIDATION=1`, `PATHWAYS_PYTHON`,
`PATHWAYS_DATA_ROOT`, `PYPSA_PYTHON`.

`pypsa_toy` is the only model that lives in this repo, so rule 1 does not apply
to it — but `adapters/pypsa_toy/network.py` is a *versioned artifact*: changing
it invalidates every recorded expectation and every fixture label.

## Interpreters on this machine

| What | Path | Has |
|---|---|---|
| default `python3` | `/opt/homebrew/opt/python@3.14/bin/python3.14` | pytest, numpy, matplotlib. **No pandas/pypsa.** |
| miniforge base | `~/miniforge3/bin/python` (3.10) | pandas, numpy, scipy, pypsa, linopy, highspy, xarray, yaml, pytest |
| pathways env | `~/miniforge3/envs/agentic-pathways/bin/python` (3.10) | gurobipy, pandas, scipy, geopandas, shapely |

The framework and the tests are **stdlib-only on purpose** and must pass on
both `python3` and `~/miniforge3/bin/python`. A test that needs pandas or pypsa
must `pytest.importorskip` so the homebrew run stays green.

```bash
cd /Users/kaarthigeswaranagnapathy/Documents/GitHub/agentic-optimization && python3 -m pytest tests -q
cd /Users/kaarthigeswaranagnapathy/Documents/GitHub/agentic-optimization && ~/miniforge3/bin/python -m pytest tests -q
```

No test may invoke a solver, import a model, or call the real `claude` binary.
Subprocess behaviour is tested against stub executables.

## The guardrail, restated

Every config key belongs to exactly one tier, declared by the adapter's
`intervention_spec()`:

- **Tier A** — solver numerics (mipgap, time limit, threads, method, labels).
  Auto-applied. They change the search, not the problem.
- **Tier B** — sanctioned parameters (prices, capex, demand scale, scenario
  choice, horizon). Auto-applied **and flagged** in the record.
- **Tier C** — policy constraints (a carbon cap, an RE floor, "all demand must
  be served", a reserve margin). **Never auto-applied.** The loop stops and
  returns `needs_human`.
- **Unknown keys default to Tier C**, so a model the framework has never seen
  fails safe.

An infeasible run whose only fix is Tier C is a *result*, not a failure. Report
the trade-off; do not go looking for a way to make it solve. Never edit a policy
key mid-run.

`Supervisor(adapter, enforce_guardrail=False)` and
`apply_refinements(..., enforce=False)` are the experiment's ablation, not an
escape hatch — never reach for them to get a run through. Never run the guarded
and unguarded conditions from the same `Supervisor` instance; the condition is
recorded on `SupervisorResult.enforce_guardrail`.

## Skills and when to use them

`.claude/skills/<name>/SKILL.md` — all model-agnostic; each resolves the model
through `get_adapter()` and learns its schema from `describe_config()`.

| Skill | Use it when |
|---|---|
| `scenario-builder` | a modeling goal in words needs to become a validated config |
| `model-runner` | a config is ready to execute |
| `run-monitor` | a solve is in flight and you need to decide whether to wait, nudge Tier A, or abort |
| `log-analyzer` | a run was INFEASIBLE / hit the limit / errored |
| `output-analyzer` | a run solved and the results need a sanity check |
| `refiner` | a diagnosis needs to become a config change (this is where the guardrail lives) |
| `supervisor` | the whole loop should run to a conclusion unattended |

These same files double as the **system prompts** of the headless driver
(`framework/agent_driver.py`), so editing a skill changes the experiment's
prompt. Note it in the protocol when you do.

## Commands worth knowing

```bash
# watch or stop a running solve
python -m framework.watch <run_dir> [--follow | --json | --abort "reason"]

# the study
python -m eval list-tasks --adapter garuda
python -m eval run --adapter garuda --llm claude-cli --guardrail both \
    --seeds 5 --tasks all --max-iters 5 --solvers highs,none
python -m eval score eval_runs --report
python -m eval report eval_runs
```

`--dry-run` enumerates the cells and validates every task config through the
real adapter **without solving anything** — the right first command on a new
machine. A cell is resumable: an existing `cell.json` is reused unless
`--overwrite`.

`eval_runs/` layout: `<adapter>/<llm>/<guardrail>/<task>/seed<k>/` holding
`task.json`, `prompt.md`, `iterNN_<confighash>/` (each with `run_record.json`,
`solver.log`, `outputs/`, and `llm/<role>_<n>.json` — every prompt and reply the
driver sent, for audit) and `cell.json`. The solve cache lives in
`eval_runs/_cache/<adapter>/<config_hash>/`; delete it to force re-solves. Only
settled results are cached (OPTIMAL / INFEASIBLE / TIME_LIMIT / ERROR with a
preflight or solver origin) — runtime errors may be transient.

Headless runs go through `framework.agent_driver.AgentDriver` with a
`framework.llm` client. The `claude` CLI is invoked only through
`ClaudeCLIClient`; it owns authentication and the framework never sees a key.

## The colo node

`ssh pwrlab` — 72 cores, 187 GB RAM, **20 GB free disk**, Julia 1.12.7, Gurobi
11.0.1 at `/opt/gurobi1101`, `~/gurobi.lic`, python 3.12 with no conda. It has
`~/garuda` (an older main) and no Zenodo data. `RemoteGarudaAdapter` and
`RemotePathwaysAdapter` ship a config there over SSH and rsync the CSVs back.
Neither remote backend has been exercised against a live node — treat the first
run as a bring-up, and do not modify anything already on that box.

## Where things are

```
framework/     contract, tiers, refine, runner, supervisor, registry, analyze,
               process + monitor + watch (live monitoring), llm + agent_driver
adapters/      garuda/, pathways/, pypsa_toy/  (self-register on import)
models/        garuda, pathways — submodules, NEVER modified
examples/<a>/  smoke_test.py, demos, fixtures/ (labelled), benchmark/ (the study)
eval/          tasks, cells, cache, scoring, report, testing, __main__ (the CLI)
tests/         solver-free unit tests; run on both interpreters
runs/          run directories (gitignored)
eval_runs/     study output (gitignored)
```
