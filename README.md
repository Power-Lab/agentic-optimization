# agentic-optimization

A **model-agnostic agentic supervisory framework** for iterative energy-system
optimization. LLM agents do the labor-intensive workflow glue — build scenario →
run → watch the solve → read logs → read outputs → refine → iterate — while the
optimization model and solver stay fully deterministic.

> **New here / a modeler, not an engineer?** Start with the
> **[User Guide](docs/USER_GUIDE.md)** — a long, plain-language walkthrough of what
> this is, how to use it (by talking to it or from Python), and what every file
> means. Driving a Claude Code session in this repo? Read
> [CLAUDE.md](CLAUDE.md).

This repo is the prototype for the *Agentic AI for Iterative Energy System
Modeling* concept, and the system under test in the guardrail study described in
[docs/EXPERIMENTAL_PROTOCOL.md](docs/EXPERIMENTAL_PROTOCOL.md). All six
concept-note tasks are implemented as composable agent skills:

> scenario-builder → model-runner (+ run-monitor) → log-analyzer /
> output-analyzer → **guardrailed refiner** → supervisor (closed loop)

Six adapters plug into the same contract, skills and guardrail — they are
*examples of models that plug in*, not the framework:

| Adapter | Model | Solver | Cost of one run |
|---|---|---|---|
| `garuda` | zonal capacity-expansion / dispatch for Indonesia (`models/garuda`, submodule) | HiGHS or Gurobi | dispatch LP: seconds–minutes; expansion MILP: minutes–hours |
| `captive` | captive industrial power capacity expansion for Indonesia (`models/captive_indonesia`, submodule) | Gurobi | minutes–hours |
| `storage` | WECC storage bidding and dispatch (`models/energy_econ_storage`, submodule) | HiGHS + Gurobi | long batch (90 periods) |
| `resource_adequacy` | Northeast China UCED resource adequacy (`models/resource_adequacy`, submodule) | Gurobi | very long batch (54 cases) |
| `pathways` | provincial hourly capacity-expansion + dispatch for China (`models/pathways`, submodule) | Gurobi only | minutes (5 days) to ~20 h (full year) |
| `pypsa_toy` | a deterministic 3-bus LP that lives in this repo | HiGHS | 1–3 seconds |

The four Power Lab research models are `garuda`, `captive`, `storage`, and
`resource_adequacy`. `pathways` and `pypsa_toy` remain additional portability
and reproducibility cases. The legacy batch scripts for `storage` and
`resource_adequacy` are deliberately exposed only at their real upstream
granularity; the adapter does not pretend that unsupported per-case controls
exist.

`pypsa_toy` is the reproducible leg: no licence, no external data, seeded
profiles, so the whole study can be rerun end to end on a laptop.

## Why a framework, not a one-off

The generalizable, publishable artifacts are deliberately kept model-agnostic:

1. **The run-record contract** (`framework/run_record.py`) — the single JSON
   artifact every skill reads/writes, which also serves as the iteration ledger.
2. **The tiered intervention taxonomy** (`framework/interventions.py`,
   `framework/refine.py`) — the scientific-integrity guardrail. A change is
   Tier A (numeric → auto), Tier B (sanctioned parameter → auto + flag), or
   Tier C (relaxes a policy constraint → **human sign-off, never silent**).
   Unknown keys default to Tier C, and an adapter can escalate a *relaxing
   transition* of a Tier B key to C (`transition_rules`) so that, for example,
   leaving a no-coal scenario is never auto-applied.
3. **A thin adapter interface** (`framework/adapter.py`) — five methods a model
   implements to plug in — plus an **adapter registry** (`framework/registry.py`)
   so the skills resolve the *active* adapter (`get_adapter()`) and never name a
   model. Adapters self-register on import; with multiple registered you must set
   `AGENTIC_ADAPTER` (or pass `name=`) to choose.
4. **The closed supervisory loop** (`framework/supervisor.py`) — runs the
   iterate loop with a config-hash dedup, stopping conditions, and in-loop
   guardrail enforcement (a Tier-C proposal halts for a human; it cannot be
   laundered through the loop).
5. **A live run monitor** (`framework/process.py`, `framework/monitor.py`) — the
   solver log is teed and parsed *while it runs*, so progress, stalls,
   time-limit-near and infeasibility are observable before the process exits.
6. **A headless provider seam** (`framework/llm.py`, `framework/agent_driver.py`)
   — the same six skills run unattended against any LLM client, which is what
   makes the guardrail measurable rather than anecdotal.

## Layout

```
framework/              model-agnostic core: contract, taxonomy, registry, runner,
                        refine, analyze, supervisor, process/monitor, llm/driver
adapters/garuda/        reference adapter (local + remote backends)
adapters/captive/       Indonesia captive-power model adapter
adapters/storage/       WECC storage batch adapter
adapters/resource_adequacy/ Northeast China UCED batch adapter
adapters/pathways/      second real model, via a per-run workspace shim
adapters/pypsa_toy/     the in-repo reproducible model (network + runner + adapter)
models/                 five external model repositories as submodules, NEVER modified
.claude/skills/         the agent skills (model-agnostic; resolve via the registry)
examples/<adapter>/     per-model drivers: smoke test, demos, fixtures, benchmark
eval/                   the study's benchmark loader, cell runner, cache, scorer
tests/                  unit tests — no solver, no model, no network
```

Everything under `framework/` and `.claude/skills/` is model-agnostic: no model
name, filename or config key appears there. Model specifics live in
`adapters/<name>/` and `examples/<name>/`.

## Setup

```bash
git submodule update --init --recursive        # fetch the model submodules
pip install -e .                                # framework (stdlib only)
```

Each model brings its own toolchain, and each runs in a **subprocess under its
own interpreter**, so nothing heavy has to be installed alongside the framework:

```bash
# garuda: Julia + HiGHS (Gurobi optional)
julia --project=models/garuda models/garuda/bootstrap.jl
# captive/storage/resource_adequacy: use each model's Julia environment;
# storage and resource_adequacy require a working Gurobi licence
# pypsa_toy: any interpreter with pypsa + highspy
pip install "pypsa>=0.30" highspy && export PYPSA_PYTHON=$(which python)
# pathways: a conda env with gurobipy/geopandas + the Zenodo data
export PATHWAYS_PYTHON=~/miniforge3/envs/agentic-pathways/bin/python
export PATHWAYS_DATA_ROOT=/path/to/AdvAppliedEnergy_Pathways_2025
```

See `examples/pathways/README.md` and `examples/pypsa_toy/README.md` for the
per-model detail.

## Verify

```bash
python -m pytest tests/ -q                    # contract/guardrail/monitor/driver/eval
python examples/pypsa_toy/smoke_test.py       # a real solve in ~1 s, no licence
python examples/garuda/smoke_test.py          # the model's own CI regression case (HiGHS)
python examples/garuda/demo_refine_loop.py    # guardrail: refuse a Tier-C cap relax
python examples/garuda/demo_supervisor.py     # closed loop halts for human sign-off
```

The garuda smoke test runs the model's committed regression case
(`maluku` 2030 dispatch on HiGHS) and checks a `solver.log` was captured, the run
is OPTIMAL, and the three headline numbers match the model's baseline within 1 %.

## Watching a run

`python -m framework.watch runs/<name> --follow` tails a solve through the live
monitor (progress, incumbents, stalls, time-limit-near, infeasible, tracebacks);
`--json` for scripts, `--abort "reason"` to stop it. In code:
`run_and_record(adapter, config, run_dir, on_event=print)`. The rolling summary
lands in `run_record.json` under `execution.monitor` and in
`<run_dir>/monitor.json`. See `.claude/skills/run-monitor/SKILL.md`.

## Headless driver (no interactive agent)

`framework.agent_driver.AgentDriver` runs the four reasoning skills
(scenario-builder / log-analyzer / refiner / output-analyzer) against any
`framework.llm.LLMClient`, using each skill's own `SKILL.md` as the system prompt
and answering in JSON against a per-role schema. The driver performs every side
effect; the model only reasons.

```python
from framework import AgentDriver, Supervisor, StopCriteria, get_adapter, make_client
from framework.agent_driver import make_propose_fn

adapter = get_adapter("garuda")
driver = AgentDriver(make_client("claude-cli"), adapter)      # or make_client("fake")
result = Supervisor(adapter).run(config, make_propose_fn(driver),
                                 run_root="runs/<goal>", stop=StopCriteria(max_iters=5))
```

Every prompt and reply is written to `<run_dir>/llm/<role>_<n>.json`. The
provider is a swappable factor: `make_client("claude-cli" | "claude-cli:opus" |
"fake" | "anthropic")`. Authentication belongs to the CLI — the framework never
sees a key.

## Running on a remote node (colo)

For models larger than the local machine handles comfortably, the same adapter
interface has a remote backend (`adapters/garuda/remote_adapter.py`,
`RemoteGarudaAdapter`). It ships the config over SSH, runs the solver on a
compute node with Julia + Gurobi already set up, captures the log, and rsyncs the
result CSVs back — the framework, skills, and guardrail are unchanged. Probe a
node first:

```bash
python examples/garuda/remote_check.py --host pwrlab --remote-root '~/garuda'
```

## Evaluation harness (`eval/`)

The guardrail study's benchmark and scorer (`docs/EXPERIMENTAL_PROTOCOL.md`
§4–8). A *task* is a labelled config under
`examples/<adapter>/benchmark/<task_id>/`; a *cell* is one
(task × adapter × LLM × guardrail × seed) run of the supervisor loop driven by
the headless driver.

```bash
python -m eval list-tasks --adapter garuda
python -m eval run --adapter garuda --llm claude-cli --guardrail both \
    --seeds 5 --tasks all --max-iters 5 --solvers highs,none
python -m eval score eval_runs --report
```

Results land in `eval_runs/` (gitignored); `python -m eval report eval_runs`
writes the metric tables and the headline figure (GVR by condition × model) to
`eval_runs/report/`. Identical configs are solved once — the solve cache is keyed
on `config_hash` — so seeds and conditions are cheap after the first pass.
Benchmarks: 15 garuda tasks (3 per family) and 5 pypsa_toy tasks (1 per family).

## The guardrail in one example

An infeasible run whose only real fix is loosening the carbon cap is **not**
auto-fixable. The refiner classifies a `CO2_limit` relaxation as Tier C, refuses
to apply it, records the attempt in the run record's audit trail, and surfaces
the trade-off to a human. That refusal is the point — a feasible-but-wrong
answer is worse than an honest infeasibility.

The experiment measures exactly that: `Supervisor(adapter,
enforce_guardrail=False)` is the ablation, and it is the *only* behavioural
difference between conditions.
