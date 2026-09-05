# pypsa_toy — the reproducible leg of the study

`garuda` and `pathways` are real research models: they carry licences, institutional
datasets, multi-hour solves and, in one case, a Gurobi requirement. That makes them
the right thing to *validate* the framework against and the wrong thing to ask a
reviewer to rerun.

`pypsa_toy` is the answer to that. It is a deterministic 3-bus capacity-expansion LP
built with [PyPSA](https://pypsa.org) and solved by HiGHS: no licence, no external
data, no cluster. Every profile comes from a fixed seed, so the same config produces
the same LP and the same answer on any machine. The full 14-day case solves in one
to three seconds; a 1-day case in well under one. The entire guardrail experiment
can be rerun on a laptop.

It is a toy, but not a strawman. It has hourly dispatch and investment in one LP, a
meshed network with expandable lines, storage with a state-of-charge balance, a
must-run thermal unit, a carbon cap, and a renewable-share floor — which is exactly
the set of features that make the Tier-C question real.

## Setup

You need an interpreter with `pypsa`, `linopy` and `highspy`. On this machine that
is the miniforge base environment, which the adapter finds by itself:

```bash
~/miniforge3/bin/python -c "import pypsa, linopy, highspy; print(pypsa.__version__)"
```

Anywhere else, point `PYPSA_PYTHON` at the right interpreter:

```bash
export PYPSA_PYTHON=/path/to/python          # needs pypsa >= 0.30, linopy, highspy
```

The framework itself never imports pypsa — the adapter shells out to
`adapters/pypsa_toy/runner.py` under that interpreter — so the rest of the repo
keeps working on a bare Python.

To create a suitable environment from scratch:

```bash
python -m pip install "pypsa>=0.30" highspy
```

## The model in one screen

```
        north  (500 MW peak)                     coal_existing  300 MW, MUST-RUN at 20 %
       /     \                                   coal_new       extendable to 1 GW
 400 MW       400 MW                             gas            extendable, unlimited
     /           \
  east --------- south                           east:  wind   <= 600 MW + 4 h battery
 (300 MW)  200 MW  (200 MW)                      south: solar  <= 800 MW + 4 h battery
```

Snapshots are hourly, `snapshots_days x 24` of them, starting Monday 2030-01-07.
Annualised capital costs are pro-rated to the horizon, so a 1-day run makes the same
investment trade-offs as a 14-day one. A short horizon is a strict prefix of the long
one, which is what makes `snapshots_days` a safe knob for fast iteration.

The one fact that drives most of the fixtures: **`coal_existing` is must-run and not
extendable**. It burns 60 MW·h of coal every hour no matter what, which puts a hard
floor of ~1,289 t CO2 per modelled day on the system and a ceiling of ~0.92 on the
renewable share. No price, no capex, no horizon length and no solver setting can move
either. That is what makes a carbon cap below the floor a genuine Tier-C situation
rather than a puzzle the agent could solve if it thought harder.

## Config keys and tiers

Flat JSON, closed key set (an unknown key is an error, not a silent no-op), every key
optional — `{}` is a legal and feasible config.

| Tier | Keys | Meaning |
|---|---|---|
| **C** — policy, human sign-off | `co2_cap_t`, `re_share_min`, `allow_load_shedding` | The study's claims. Relaxing any of them answers a different question. |
| **B** — sanctioned parameters | `demand_scale`, `gas_price`, `coal_price`, `wind_capex`, `solar_capex`, `battery_capex`, `line_expansion_allowed`, `snapshots_days` | Change the answer, inside the space the modeller sanctioned. Applied automatically, always flagged. |
| **A** — numerics | `solver`, `mip_gap`, `time_limit`, `threads` | Change how HiGHS searches, never what it solves. Applied silently. |

`PypsaToyAdapter.describe_config()` prints the full version — each key's type, range,
default and physical meaning, plus the feasibility limits and the sanity anchors for
the output analyzer. That string is what the LLM-facing skills read; it is the model's
documentation, not a summary of it.

## Running things

```bash
# end-to-end regression check (build, solve, archive, verify invariants)
python examples/pypsa_toy/smoke_test.py                 # 1 day, seconds
python examples/pypsa_toy/smoke_test.py --days 14       # the full horizon
python examples/pypsa_toy/smoke_test.py --record        # print headline numbers

# one refine step: a Tier-B fix applied, a Tier-C fix withheld, then a re-run
python examples/pypsa_toy/demo_refine_loop.py

# the closed loop halting for human sign-off on an impossible carbon cap
python examples/pypsa_toy/demo_supervisor.py
python examples/pypsa_toy/demo_supervisor.py --unguarded   # the ablation condition
```

Tests are solver-free and run on any interpreter:

```bash
python3 -m pytest tests/test_pypsa_toy.py -q        # pypsa tests skip
~/miniforge3/bin/python -m pytest tests/test_pypsa_toy.py -q   # all of them run
```

## What lands where

A run directory contains everything needed to audit the run:

```
runs/<name>/
  config.json        the exact config handed to the model
  solver.log         the child's merged stdout/stderr, teed line by line
  highs.log          HiGHS' own log (the runner's fallback status source)
  summary.json       config + horizon facts + status + headline results
  run_record.json    the framework's record (written by run_and_record)
  outputs/
    generator_results.csv   storage_results.csv   line_results.csv
    cost_results.csv        emissions_results.csv nse_results.csv
```

`solver.log` opens with `[pypsa_toy] horizon <key> = <value>` lines computed for the
actual config — demand energy, renewable potential, the emissions floor and the
all-coal ceiling — so a diagnosis never has to guess whether a cap was physically
reachable. It ends with `[pypsa_toy] status: <OPTIMAL|INFEASIBLE|TIME_LIMIT|ERROR>`.

## Directories

- `fixtures/` — labelled configs with known root causes, one per failure family.
  See `fixtures/README.md`; `tests/test_pypsa_toy.py` checks the labels that can be
  checked without a solver.
- `benchmark/` — the evaluation harness's task set (see `eval/`), separate from the
  fixtures so a fixture can be edited without moving the benchmark's goalposts.
