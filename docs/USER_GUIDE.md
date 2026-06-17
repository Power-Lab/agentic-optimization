# Agentic-Optimization — User Guide for Modelers

This guide is written for **energy-system modelers**, not software engineers. It
assumes you know your optimization model (scenarios, constraints, what an
infeasible run means) and that you can run Julia/Gurobi, but it assumes **nothing**
about the agentic framework, Python packaging, or LLM agents. Every concept is
explained from scratch. It is intentionally long — skim the table of contents and
jump to what you need.

---

## Table of contents

1. [What this tool is (in plain language)](#1-what-this-tool-is-in-plain-language)
2. [The mental model: the loop, the solver, the guardrail](#2-the-mental-model)
3. [One-time setup](#3-one-time-setup)
4. [The two ways to use it](#4-the-two-ways-to-use-it)
5. [Walkthrough A — talking to it (conversational, via Claude Code)](#5-walkthrough-a--conversational)
6. [Walkthrough B — driving it from Python](#6-walkthrough-b--from-python)
7. [The six skills, one by one](#7-the-six-skills-one-by-one)
8. [The guardrail, explained for modelers](#8-the-guardrail-explained-for-modelers)
9. [The config: what you can and cannot set](#9-the-config)
10. [What you get back: outputs and the run record](#10-what-you-get-back)
11. [File-by-file reference: what every file means](#11-file-by-file-reference)
12. [Adding your own model (a new adapter)](#12-adding-your-own-model)
13. [Troubleshooting & FAQ](#13-troubleshooting--faq)
14. [Glossary](#14-glossary)

---

## 1. What this tool is (in plain language)

When you build energy-system scenarios, a lot of your time goes **not** into the
interesting modeling but into the *mechanical loop around it*:

> set up a scenario → run the solver → wait → read the log → figure out why it
> failed or looks weird → change a setting → run again → compare → repeat.

This tool puts an **AI assistant** around that loop. You describe what you want in
plain English; the assistant prepares the run, launches the **real solver**, reads
the logs and results, tells you what went wrong, proposes a fix, and can run the
loop again — while keeping a written record of everything it did.

Two things are deliberately true and important:

- **The AI never does the math.** Your optimization model and Gurobi solve exactly
  as they always did. The AI only does the *glue work* — preparing inputs, reading
  outputs, suggesting changes. The numbers come from the solver, not the language
  model. This is non-negotiable and is what makes the results trustworthy.
- **The AI is not allowed to quietly change your science.** It can loosen a solver
  tolerance on its own, but it will **refuse** to "fix" an infeasible run by
  secretly relaxing your carbon cap. Those changes are escalated to you. (Details
  in [section 8](#8-the-guardrail-explained-for-modelers).)

The bundled example is the **village-Indonesia 100 GW** capacity-expansion model,
but the framework is **model-agnostic**: your own model can be plugged in (see
[section 12](#12-adding-your-own-model)). Throughout this guide, "the model" means
whichever model is currently active; the village model is used in all examples.

---

## 2. The mental model

Picture three layers stacked on top of each other:

```
┌─────────────────────────────────────────────────────────────┐
│  YOU  — "build a coordinated Maluku 2030 case and run it"     │
└───────────────────────────┬───────────────────────────────────┘
                            │  plain English
┌───────────────────────────▼───────────────────────────────────┐
│  THE SKILLS (the AI assistant's playbook)                       │
│  scenario-builder · model-runner · log-analyzer ·               │
│  output-analyzer · refiner · supervisor                         │
│  — they decide WHAT to do, and call the framework to do it      │
└───────────────────────────┬───────────────────────────────────┘
                            │  Python function calls
┌───────────────────────────▼───────────────────────────────────┐
│  THE FRAMEWORK (deterministic plumbing — no AI here)            │
│  validate · run the solver · capture logs · record results ·    │
│  classify a proposed change · enforce the guardrail             │
└───────────────────────────┬───────────────────────────────────┘
                            │  subprocess
┌───────────────────────────▼───────────────────────────────────┐
│  YOUR MODEL + SOLVER (Julia / JuMP / Gurobi — unchanged)        │
└─────────────────────────────────────────────────────────────┘
```

The **loop** the assistant runs is:

```
build scenario → run model → read log → read outputs → propose refinement → run again → …
```

…and it stops when the run is satisfactory, when it gets stuck, or when the only
remaining fix is one it is not allowed to make on its own.

Keep this picture in mind: **the skills are the brain, the framework is the hands,
your model is the calculator.** The brain is the only part that "thinks," and it is
fenced in by the framework so it can't overstep.

---

## 3. One-time setup

You need: **Git**, **Python ≥ 3.9**, **Julia**, and a **working Gurobi licence**
(the village model is too large for the free size-limited licence — an academic
licence works).

```bash
# 1. Get the code and the model (the model is a git "submodule" — a pinned copy
#    of the village repo that lives under models/village/)
git clone https://github.com/Power-Lab/agentic-optimization.git
cd agentic-optimization
git submodule update --init --recursive

# 2. Install the Python framework and its (tiny) dependencies
pip install -e .

# 3. One-time: prepare the model's Julia environment (installs Julia packages,
#    checks that Gurobi can start). Takes ~1 minute the first time.
julia --project=models/village models/village/bootstrap.jl
```

Confirm everything works end-to-end with the **smoke test** — it runs the smallest
real scenario (Maluku 2030) and checks the answer matches a known-good baseline:

```bash
python examples/village/smoke_test.py
```

You should see `✅ ALL CHECKS PASSED`. If you do, the framework can drive your
solver. If not, jump to [Troubleshooting](#13-troubleshooting--faq).

> **What is a "submodule"?** `models/village/` is not a copy you edit — it's a
> reference to a specific commit of the village model's own repository. The
> framework never modifies it. This keeps a clean line between "the framework" and
> "the model it happens to be driving."

---

## 4. The two ways to use it

There are two front doors. Most modelers will use the first.

**A. Conversational (recommended).** Open this folder in **Claude Code** and just
talk to it. The six *skills* are instructions the assistant already knows; when you
say "build a high-import-price scenario and run it," it picks the right skills and
does it. You never write Python. → [Walkthrough A](#5-walkthrough-a--conversational).

**B. Programmatic.** If you want to script runs, embed the loop in a notebook, or
run unattended sweeps, you can call the framework directly from Python. The example
scripts under `examples/village/` show exactly how. → [Walkthrough B](#6-walkthrough-b--from-python).

Both front doors use the **same framework underneath**, so they behave identically
and obey the same guardrail.

---

## 5. Walkthrough A — conversational

Open the project in Claude Code and type naturally. Here is a realistic session.
The **bold** lines are what you type; the rest is what the assistant does.

**"Build a standalone-village and a coordinated grid+village case for Maluku 2030,
then run both."**
- The **scenario-builder** skill reads your model's options, maps "standalone
  village" → `scenario: village` and "coordinated" → `scenario: gridvillage`,
  fills in the rest of the config, and validates both.
- The **model-runner** skill runs each one through the solver, capturing the log
  and writing a results folder per run.

**"Why did the coordinated one fail?"**
- The **log-analyzer** skill opens that run's `solver.log`, sees the termination
  status, and tells you the root cause in one sentence — e.g. "infeasible because
  the CO₂ cap for Maluku 2030 is below what any feasible build can achieve," citing
  the relevant log lines.

**"Can you fix it?"**
- The **refiner** skill proposes the smallest change that could help. If that's a
  numeric setting (e.g. loosen the MIP gap), it applies it and re-runs. If the only
  real fix is loosening the carbon cap, it **stops and asks you**, explaining the
  scientific trade-off — it will not do that silently.

**"Are the results from the standalone run realistic?"**
- The **output-analyzer** skill reads the result CSVs and flags anything
  implausible — unmet demand above tolerance, a cost component orders of magnitude
  off, suspiciously zero battery build — or tells you it all looks sensible.

**"Keep iterating until this solves."**
- The **supervisor** skill runs the whole loop autonomously: run → diagnose →
  refine → run again, never repeating a setting it already tried, stopping when it
  solves, gets stuck, or hits a change it's not allowed to make on its own.

You can be this terse. The skills carry the detailed operating rules so you don't
have to.

---

## 6. Walkthrough B — from Python

Everything the assistant does, you can do directly. The key idea: you **never name
the model in code** — you ask the registry for the *active* adapter, and it hands
you the right one.

```python
from framework import get_adapter, run_and_record

adapter = get_adapter()          # resolves the active model (here: "village")
print(adapter.describe_config()) # prints the schema: keys, scenarios, levers

# 1. Define a scenario (a plain dict — see section 9 for the keys)
config = {
    "island": "maluku", "year": "2030", "scenario": "gridvillage",
    "clean": "reference", "CO235reduction": False,
    "BAUCO2emissions": 0.0, "CO2_limit": 5820000,
}

# 2. Validate before spending solver time
result = adapter.validate_config(config)
assert result.ok, result.errors

# 3. Run it — this launches the real solver and records everything
record = run_and_record(adapter, config, run_dir="runs/my_first_run")
print(record.execution.termination_status)   # OPTIMAL / INFEASIBLE / TIME_LIMIT / ERROR
print(record.execution.wall_seconds)
```

Running the **closed loop** yourself:

```python
from framework import Supervisor, StopCriteria, ProposedChange

def propose(record):
    # Your refinement logic. Look at record.execution / record.log_diagnosis and
    # return the smallest sensible change. Return [] to give up.
    if record.execution.termination_status == "TIME_LIMIT":
        return [ProposedChange("mipgap", None, 0.05)]
    return []

result = Supervisor(adapter).run(config, propose,
                                 run_root="runs/loop", stop=StopCriteria(max_iters=5))
print(result.outcome, result.iterations)      # e.g. "solved" 2
```

The supervisor enforces the guardrail for you: if `propose` ever returns a policy
relaxation (Tier C), the loop **halts with `needs_human`** instead of applying it.

The runnable, commented versions of all of this are:
- `examples/village/smoke_test.py` — run one scenario, check against baseline.
- `examples/village/demo_refine_loop.py` — the guardrail refusing a cap relaxation.
- `examples/village/demo_supervisor.py` — the closed loop halting for sign-off.

---

## 7. The six skills, one by one

Each skill is a short instruction file under `.claude/skills/<name>/SKILL.md`. You
don't run these directly — the assistant invokes them. Knowing what each does helps
you ask for the right thing.

| Skill | Concept-note task | What it does | When it triggers |
|-------|-------------------|--------------|------------------|
| **scenario-builder** | 1. Build scenario | Turns your words into a validated config | "build / set up a scenario", "add a cap", "sweep X" |
| **model-runner** | 2. Execute | Runs one config through the solver, captures the log + results | "run this", or as a loop step |
| **log-analyzer** | 3. Analyze logs | Diagnoses a failed/odd run from its `solver.log` | a run is INFEASIBLE / errored / hit the time limit |
| **output-analyzer** | 4. Analyze outputs | Flags implausible patterns in a *successful* run's results | "are these results realistic?", "why are emissions so high?" |
| **refiner** | 5. Refine | Proposes & applies a *controlled* change, escalates policy changes | "fix it", "adjust to reduce cost" |
| **supervisor** | 6. Iterate | Runs the whole loop autonomously with dedup + stopping rules | "iterate until it solves", "drive this to convergence" |

A natural division of labor: **log-analyzer** handles runs that *failed*;
**output-analyzer** handles runs that *succeeded but might be wrong*; **refiner**
acts on either diagnosis; **supervisor** chains them all together.

---

## 8. The guardrail, explained for modelers

This is the most important section for trusting the tool.

When the refiner wants to change your config, the framework sorts the change into
one of **three tiers**. Which key falls in which tier is declared by your model's
adapter, so it reflects *your* model's meaning.

- **Tier A — numeric settings (applied automatically).** Things that change *how the
  solver searches*, not *what problem it solves*: the MIP gap, tolerances, a time
  limit. Loosening the MIP gap from 1% to 5% gives a slightly less tight solution
  faster — it does not change what scenario you asked. The agent may do this on its
  own. *(Village example: `mipgap`.)*

- **Tier B — sanctioned parameters (applied, but flagged).** Choices you have
  explicitly put on the table: switching among the allowed scenario names, changing
  an import price, raising a per-unit storage cap. These change the answer, but stay
  inside the space you sanctioned. The agent applies them and tells you.
  *(Village example: `scenario`, `import_price`, `village_storage_max_mwh`.)*

- **Tier C — policy constraints (NEVER applied silently).** Anything that *relaxes a
  policy target*: loosening a carbon cap, lowering a renewable-share floor, lifting
  a coal ban, turning off the clean constraint. These change the *meaning of the
  study*. An agent that "fixes" an infeasible run by quietly raising your CO₂ cap
  has produced a feasible answer to a **different question** — which is worse than an
  honest "infeasible." The framework refuses to auto-apply these; it records the
  proposal and escalates to you with the trade-off spelled out.
  *(Village example: `clean`, `CO2_limit`, `RE_limit`, `CO235reduction`, `BAUCO2emissions`.)*

Two safety defaults worth knowing:
- **Unknown keys default to Tier C.** If the agent ever proposes changing a key the
  adapter didn't classify, the framework treats it as policy-grade and refuses to
  auto-apply it. The safe default is "don't silently touch anything unsanctioned."
- **Illegal values are rejected outright.** Proposing `scenario: frobnicate` (not in
  the allowed list) is rejected before anything runs, regardless of tier.

Every proposal — applied, blocked, or rejected — is written into the run's audit
trail (`refinement_history` in `run_record.json`), with the before/after value and
the reason. You can always reconstruct exactly what was changed and why.

This is enforced in **code** (`framework/refine.py` + `framework/interventions.py`),
not by asking the AI nicely. The AI cannot bypass it.

---

## 9. The config

A "config" is a small dictionary (saved as `config.json`) describing one run. To see
the authoritative, live description for the active model, run
`get_adapter().describe_config()` or ask the assistant "what can I set?". For the
**village model** it is:

**Required keys**

| Key | Meaning | Example |
|-----|---------|---------|
| `island` | which island's data folder to use | `"maluku"` |
| `year` | model year (string) | `"2030"` |
| `scenario` | which scenario configuration (see below) | `"gridvillage"` |
| `clean` | `"reference"` (no climate constraints) or `"clean"` (enforce CO₂ cap + RE floor) | `"reference"` |
| `CO235reduction` | enable the 2035 reduction logic (usually `false`) | `false` |
| `BAUCO2emissions` | business-as-usual emissions baseline (tCO₂) | `0.0` |
| `CO2_limit` | the CO₂ cap (tCO₂) — only enforced when `clean: clean` | `5820000` |

**Legal `scenario` values** (the model's scenario "shapes"):

| Scenario | Grid expansion | Village build | Notes |
|----------|:--:|:--:|-------|
| `base` | no | no | grid only, no village investment |
| `grid` | yes | no | grid expansion only |
| `village` | no | yes | standalone village systems |
| `gridvillage` | yes | yes | coordinated grid + village |
| `nocoal` | yes | no | coal banned |
| `highimportprice` | yes | yes | higher village import price |
| `captive`, `gridcaptive` | — | — | legacy aliases for `village` / `gridvillage` |

**Optional passthrough keys** (omit to use model defaults):

| Key | Tier | Meaning |
|-----|:--:|---------|
| `mipgap` | A | relative MIP gap (default 0.01) |
| `import_price` | B | $/MWh for village grid imports |
| `village_storage_max_mwh` | B | per-unit cap on new village storage |
| `RE_limit` | C | minimum renewable share (clean runs) |

**Important reality check:** there is **no "X% solar" knob.** Renewable penetration
is an *outcome* the solver chooses given costs and resources, not an input you set.
To bias it, you change costs or input data — the scenario-builder will tell you this
rather than invent a key that doesn't exist.

---

## 10. What you get back

Each run produces a folder (you choose where, e.g. `runs/my_run/`) containing:

- **`config.json`** — the exact config that was run.
- **`solver.log`** — everything the solver printed (Gurobi's iteration log + the
  termination status). This is what the log-analyzer reads. *(Note: the model itself
  doesn't write a log file — the framework captures the solver's console output for
  you.)*
- **`outputs/`** — the model's result CSVs, copied here so the run is a permanent,
  self-contained record. For the village model these include `cost_results.csv`,
  `generator_results.csv`, `village_generator_results.csv`, `nse_results.csv`
  (non-served energy = reliability), `clean_energy_results.csv` (emissions + RE
  share), and more. See `models/village/docs/outputs_guide.md` for what each column
  means.
- **`run_record.json`** — **the most important file.** It is the single record that
  ties everything together and the only thing the skills pass to one another.

### Anatomy of `run_record.json`

```jsonc
{
  "config": { ... },                  // the config that was run
  "config_hash": "b86fd591bc25842f",  // a short fingerprint of the config
  "execution": {
    "termination_status": "INFEASIBLE",  // OPTIMAL / TIME_LIMIT / INFEASIBLE / ERROR
    "wall_seconds": 21.9,
    "solver_log": "solver.log",
    "returncode": 1,
    "error_origin": "solver"            // preflight / solver / runtime / remote
  },
  "log_diagnosis": {                    // written by log-analyzer
    "root_cause": "CO2 cap below physical floor …",
    "evidence": ["log:412"],
    "suggested_intervention_tier": "C"
  },
  "output_anomalies": [                 // written by output-analyzer
    { "metric": "Total_NSE_MWh", "value": 1234, "expected": "~0", "severity": "high" }
  ],
  "refinement_history": [               // written by refiner — the AUDIT TRAIL
    { "tier": "A", "change": {"mipgap": [0.01, 0.05]}, "rationale": "…", "applied": true }
  ],
  "parent_run": "…"                     // hash of the run this was refined from
}
```

Reading this file (or asking the assistant to summarize it) tells you the complete
story of a run: what was tried, what happened, what was concluded, and what was
changed. In a loop, the chain of `parent_run` links is your iteration ledger.

---

## 11. File-by-file reference

You can use the tool without ever opening these. This section is for when you want
to understand or extend it. The **"touch?"** column tells you whether a modeler
normally edits the file.

### Top level

| File | Touch? | What it is |
|------|:--:|------------|
| `README.md` | read | Short overview + quick commands. |
| `docs/USER_GUIDE.md` | read | This document. |
| `pyproject.toml` | rare | Python package definition + dependencies (`click`, `pyyaml`). Edit only to add a dependency. |
| `.gitmodules` | no | Declares the `models/village` submodule. |
| `.gitignore` | no | Keeps run artifacts / caches out of git. |

### `framework/` — the model-agnostic core (no AI, no model specifics)

| File | Touch? | What it is |
|------|:--:|------------|
| `__init__.py` | no | Re-exports the public API so you can `from framework import …`. |
| `run_record.py` | no | Defines `RunRecord` and its parts (`Execution`, `Diagnosis`, `Anomaly`, `Refinement`) — the contract from [section 10](#10-what-you-get-back). Also `config_hash`. |
| `adapter.py` | no | The `Adapter` base class: the four methods any model must implement (`validate_config`, `run`, `intervention_spec`, `locate_outputs`) plus `describe_config`. **Read this if you'll add a model.** |
| `interventions.py` | rare | The **tiered guardrail**: `Tier` (A/B/C), `InterventionSpec` (which key is which tier), and `decide()` (classify one proposed change). The heart of [section 8](#8-the-guardrail-explained-for-modelers). |
| `refine.py` | no | `apply_refinements()` — takes proposed changes, classifies each via `decide()`, applies Tier A/B, **blocks Tier C**, and writes the audit trail. Enforcement lives here. |
| `runner.py` | no | `run_and_record()` — runs preflight, executes via the adapter, writes `run_record.json` even on failure. |
| `analyze.py` | no | Output-analysis helpers: `read_outputs()` (load result CSVs through the adapter) and `record_anomalies()`. |
| `supervisor.py` | no | `Supervisor` — the closed loop: dedup by `config_hash`, stopping conditions, and in-loop guardrail enforcement. Returns a `SupervisorResult`. |
| `registry.py` | no | `get_adapter()` / `register_adapter()` — how skills find the active model **without naming it**. |

### `adapters/` — model plugins (one folder per model)

| File | Touch? | What it is |
|------|:--:|------------|
| `__init__.py` | when adding a model | Imports each adapter so it self-registers. |
| `village/__init__.py` | no | Registers the village adapter as `"village"`; exports its classes. |
| `village/village_adapter.py` | when adapting | The village adapter: maps the four interface methods to the real model — config schema, running `julia run_model.jl`, the Tier A/B/C lists, output locations, and `describe_config`. **The clearest template for a new model.** |
| `village/remote_adapter.py` | no (HPC later) | Same interface, but runs the solver on a remote node and pulls results back. Used for the colo/HPC path. |
| `village/transport.py` | no | How the remote adapter talks to a node: `SshTransport` (real node) vs `LocalTransport` (loopback, for testing without a node). |

### `examples/village/` — the case-study drivers (runnable scripts + test data)

| File | Touch? | What it is |
|------|:--:|------------|
| `smoke_test.py` | run it | Runs Maluku 2030 live and checks the result matches the committed baseline. Your "is everything wired up?" check. |
| `demo_refine_loop.py` | run it | Live demo: an infeasible run, diagnosed, with the refiner **refusing** to relax the carbon cap. Shows the guardrail. |
| `demo_supervisor.py` | run it | Live demo: the closed loop halting for human sign-off on the same infeasible case. |
| `remote_check.py` | HPC later | Probes a remote node (reachable? Julia? Gurobi? repo present?). |
| `remote_local_test.py` | run it | Tests the *remote* machinery locally (loopback) — no node needed. |
| `fixtures/` | extend | Deliberately-broken configs with **known** root causes, used to check the analyzers actually work. Each has a `config.json` and an `expected.json` (what should happen). See `fixtures/README.md`. |

### `.claude/skills/` — the AI assistant's playbook

| File | Touch? | What it is |
|------|:--:|------------|
| `<skill>/SKILL.md` | rare | The operating instructions for each of the six skills ([section 7](#7-the-six-skills-one-by-one)). Plain Markdown; edit to change how the assistant behaves. All are model-agnostic — they resolve the model via `get_adapter()`. |

### `tests/` — automated checks (no solver needed; run in milliseconds)

| File | Touch? | What it is |
|------|:--:|------------|
| `test_guardrail.py` | no | Pins the tier classification and the **critical** "Tier C is never auto-applied" behavior. |
| `test_supervisor.py` | no | Drives the loop with a fake model: convergence, cycle detection, exhaustion, the Tier-C halt. |
| `test_analyze.py` | no | Output-analysis plumbing. |
| `test_fixtures.py` | no | That the broken fixtures are caught as expected. |

Run them anytime with `python -m pytest tests/ -q`.

### `models/village/` — the model itself

A git submodule: the actual village-Indonesia model (Julia/JuMP/Gurobi). **The
framework never edits it.** Its own `docs/` (especially `outputs_guide.md` and the
data dictionary) document the model's inputs and outputs.

---

## 12. Adding your own model

The whole point of the framework is that the village model is *one example*. To
drive a different solver-backed model, you write one **adapter** — a Python class
implementing four methods. You do not touch `framework/` or the skills.

1. Create `adapters/<yourmodel>/<yourmodel>_adapter.py` with a class subclassing
   `framework.adapter.Adapter` and a unique `name`. Use
   `adapters/village/village_adapter.py` as the template.

2. Implement the four methods:
   - **`validate_config(config)`** → cheap checks (required keys present, legal
     values, inputs exist). Return `ValidationResult(ok=..., errors=[...])`.
   - **`run(config, run_dir)`** → write `config.json`, run your model, capture its
     console output to `run_dir/solver.log`, copy result files to
     `run_dir/outputs/`, and return an `Execution` with the termination status.
     **Do not raise on an infeasible/failed solve** — that's a normal result.
   - **`intervention_spec()`** → declare which config keys are Tier A / B / C and
     any enumerated legal values. *This is where you encode your model's
     scientific guardrail.* Be honest: anything that relaxes a policy target is
     Tier C.
   - **`locate_outputs(run_dir)`** → map output names to files so the
     output-analyzer doesn't hard-code your filenames.
   - Optionally override **`describe_config()`** with a human-readable schema.

3. Register it: in `adapters/<yourmodel>/__init__.py` call
   `register_adapter("<yourmodel>", YourAdapter)`, and import that package from
   `adapters/__init__.py`.

4. Select it: with more than one adapter installed, set the environment variable
   `AGENTIC_ADAPTER=<yourmodel>` (or pass `get_adapter("<yourmodel>")`).

That's it. All six skills, the run-record contract, and the guardrail now work with
your model unchanged. A second adapter is also what demonstrates the framework's
generality for a paper.

---

## 13. Troubleshooting & FAQ

**The smoke test fails at "Gurobi could not start" / licence error.**
You need a real Gurobi licence on this machine (`~/gurobi.lic` or
`GRB_LICENSE_FILE`). The bundled size-limited licence is too small for these models.
Academic licences are free.

**`julia: command not found` or package errors.**
Install Julia (juliaup is easiest), then re-run the one-time bootstrap:
`julia --project=models/village models/village/bootstrap.jl`.

**`models/village/` is empty.**
You skipped the submodule step: `git submodule update --init --recursive`.

**A run came back `INFEASIBLE`. Is that a bug?**
No — it's a legitimate result. Ask the assistant "why is it infeasible?" (the
log-analyzer will find the binding constraint). If the only fix is relaxing a policy
target, the refiner will surface that to you rather than do it — that's by design.

**The refiner "refused to fix" my infeasible run.**
Because the only feasibility-restoring change was a Tier C policy relaxation (e.g.
loosening the CO₂ cap). That refusal is the guardrail working. You can choose to
relax the policy yourself (and the change is recorded), but the agent won't decide
that for you. See [section 8](#8-the-guardrail-explained-for-modelers).

**`ERROR` with `error_origin: preflight`.**
The config or inputs are wrong before any solve — usually a missing required key, a
typo'd `island`/`year` with no data folder, or a bad scenario name. The message in
`solver.log` says which.

**How long should a run take?** The Maluku reference case is ~3 minutes locally
(~290k constraints). Larger islands take longer — that's what the remote/HPC path is
for (see `remote_check.py`), available later.

**Can I run several scenarios at once?** Run them in separate run dirs. Don't point
two runs at the same run dir or the same model results folder at the same time —
they'll clobber each other.

**Where did my results go?** Into the `run_dir` you specified (default examples use
`runs/<name>/`). The `runs/` folder is git-ignored on purpose — it's regenerated
output, not source.

---

## 14. Glossary

- **Adapter** — the small Python class that teaches the framework how to drive a
  specific model. Village is the example adapter.
- **Config** — a dictionary describing one run (`config.json`).
- **Guardrail** — the rule that sorts changes into Tier A/B/C and refuses to
  auto-apply policy relaxations.
- **MIP gap** — how close to optimal the solver must get before stopping; a numeric
  (Tier A) setting.
- **Preflight** — cheap validation before spending solver time.
- **Run record** (`run_record.json`) — the single file capturing a run's config,
  result, diagnosis, anomalies, and change history.
- **Skill** — one instruction file the AI assistant follows (`SKILL.md`). Six of
  them, one per task.
- **Submodule** — the pinned copy of the model under `models/village/`, never edited
  by the framework.
- **Supervisor** — the component that runs the full loop autonomously with stopping
  rules and guardrail enforcement.
- **Termination status** — the solver's verdict: `OPTIMAL`, `TIME_LIMIT`,
  `INFEASIBLE`, or `ERROR`.
- **Tier A / B / C** — numeric (auto) / sanctioned parameter (auto + flag) / policy
  (human sign-off only).
