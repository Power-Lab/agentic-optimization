# agentic-optimization

A **model-agnostic agentic supervisory framework** for iterative energy-system
optimization. LLM agents do the labor-intensive workflow glue — build scenario →
run → read logs → read outputs → refine → iterate — while the optimization model
and solver stay fully deterministic.

> **New here / a modeler, not an engineer?** Start with the
> **[User Guide](docs/USER_GUIDE.md)** — a long, plain-language walkthrough of what
> this is, how to use it (by talking to it or from Python), and what every file
> means.

This repo is the prototype for the *Agentic AI for Iterative Energy System
Modeling* concept. All six concept-note tasks are implemented as composable
agent skills:

> scenario-builder → model-runner → log-analyzer / output-analyzer →
> **guardrailed refiner** → supervisor (closed loop)

The **village-Indonesia 100 GW** capacity-expansion model is the first adapter /
case study — *one example of a model that plugs in*, not the framework itself.
A second adapter (any solver-backed model) would plug into the same skills,
contract, and guardrail unchanged.

## Why a framework, not a one-off

The generalizable, publishable artifacts are deliberately kept model-agnostic:

1. **The run-record contract** (`framework/run_record.py`) — the single JSON
   artifact every skill reads/writes, which also serves as the iteration ledger.
2. **The tiered intervention taxonomy** (`framework/interventions.py`,
   `framework/refine.py`) — the scientific-integrity guardrail. A change is
   Tier A (numeric → auto), Tier B (sanctioned parameter → auto + flag), or
   Tier C (relaxes a policy constraint → **human sign-off, never silent**).
   Unknown keys default to Tier C.
3. **A thin adapter interface** (`framework/adapter.py`) — four methods a model
   implements to plug in — plus an **adapter registry** (`framework/registry.py`)
   so the skills resolve the *active* adapter (`get_adapter()`) and never name a
   model. Adapters self-register on import; set `AGENTIC_ADAPTER` to choose when
   more than one is installed.
4. **The closed supervisory loop** (`framework/supervisor.py`) — runs the
   iterate loop with a config-hash dedup, stopping conditions, and in-loop
   guardrail enforcement (a Tier-C proposal halts for a human; it cannot be
   laundered through the loop).

## Layout

```
framework/              model-agnostic core: contract, taxonomy, registry,
                        runner, refine, analyze, supervisor
adapters/village/       the village case-study adapter (local + remote backends)
models/village/         the model itself (git submodule, never modified)
.claude/skills/         the 6 agent skills (model-agnostic; resolve via registry)
examples/village/       case-study drivers + fixtures (smoke test, demos)
tests/                  guardrail + contract + supervisor unit tests (no solver)
```

`adapters/village/` and `examples/village/` are where the model-specific code
lives; everything under `framework/` and `.claude/skills/` is model-agnostic.

## Setup

```bash
git submodule update --init --recursive        # fetch the model
pip install -e .                                # framework + deps
# one-time model env, for the village example (needs a Gurobi licence):
julia --project=models/village models/village/bootstrap.jl
```

## Verify

```bash
python -m pytest tests/ -q                       # guardrail/contract/supervisor (fast)
python examples/village/smoke_test.py            # live solve vs committed baseline
python examples/village/demo_refine_loop.py      # guardrail: refuse a Tier-C cap relax
python examples/village/demo_supervisor.py       # closed loop halts for human sign-off
```

The smoke test runs `base_maluku_2030_reference` through the adapter and checks
a `solver.log` was captured, the run is OPTIMAL, and the CSVs match the model's
committed baseline.

## Running on a remote node (colo)

For larger islands than the local machine handles comfortably, the same adapter
interface has a remote backend (`adapters/village/remote_adapter.py`,
`RemoteVillageAdapter`). It ships the config over SSH, runs the solver on a
direct-SSH compute node with Julia + Gurobi already set up, captures the log,
and rsyncs the result CSVs back — the framework, skills, and guardrail are
unchanged. Probe a node first:

```bash
python examples/village/remote_check.py --host user@colo \
    --remote-root '~/agentic-optimization/models/village' --ensure-repo
```

## The guardrail in one example

An infeasible run whose only real fix is loosening the carbon cap is **not**
auto-fixable. The refiner classifies a `CO2_limit` relaxation as Tier C, refuses
to apply it, records the attempt in the run record's audit trail, and surfaces
the trade-off to a human. That refusal is the point — a feasible-but-wrong
answer is worse than an honest infeasibility.
