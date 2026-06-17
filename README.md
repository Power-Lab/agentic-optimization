# agentic-optimization

A **model-agnostic agentic supervisory framework** for iterative energy-system
optimization. LLM agents do the labor-intensive workflow glue — build scenario →
run → read logs → read outputs → refine → iterate — while the optimization model
and solver stay fully deterministic.

This repo is the prototype for the *Agentic AI for Iterative Energy System
Modeling* concept. The first proof-of-concept slice ("scope B") is:

> scenario-builder → model-runner → log-analyzer → **guardrailed refiner**

against the village-Indonesia 100 GW capacity-expansion model as the first
adapter / case study.

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
   implements to plug in. The village model is `adapters/village/`; a second
   adapter would prove generality.

## Layout

```
framework/            model-agnostic core (contract, taxonomy, runner, refine)
adapters/village/     reference adapter → village-Indonesia model
models/village/       the model itself (git submodule, never modified)
.claude/skills/       the agent skills (scenario-builder, model-runner,
                      log-analyzer, refiner)
scripts/smoke_test.py Phase-1 gate: run the model live, diff the baseline
fixtures/             deliberately-broken configs with known root causes
tests/                guardrail + contract unit tests (no solver needed)
```

## Setup

```bash
git submodule update --init --recursive        # fetch the model
pip install -e .                                # framework + deps
# one-time model env (needs a Gurobi licence):
julia --project=models/village models/village/bootstrap.jl
```

## Verify

```bash
python -m pytest tests/ -q                      # guardrail + contract (fast)
python scripts/smoke_test.py                    # live solve vs committed baseline
```

The smoke test runs `base_maluku_2030_reference` through the adapter and checks
a `solver.log` was captured, the run is OPTIMAL, and the CSVs match the model's
committed baseline.

## The guardrail in one example

An infeasible run whose only real fix is loosening the carbon cap is **not**
auto-fixable. The refiner classifies a `CO2_limit` relaxation as Tier C, refuses
to apply it, records the attempt in the run record's audit trail, and surfaces
the trade-off to a human. That refusal is the point — a feasible-but-wrong
answer is worse than an honest infeasibility.
