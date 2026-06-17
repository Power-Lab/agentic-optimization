---
name: scenario-builder
description: Turn a natural-language modeling goal into a validated optimization config/scenario for the agentic-optimization framework. Use when the user describes a scenario to run ("build a high-solar Maluku 2030 case", "add a carbon cap", "sweep import price"). Emits a config dict the model-runner skill can execute, gated by adapter validation.
---

# Scenario Builder (Task 1)

Convert a research goal into a **validated config** for the active model adapter.
You produce config, you do not run it — hand off to the `model-runner` skill.

## Operating rules

1. **Discover the schema from the adapter, never hard-code it.** The reference
   adapter is `adapters/village/village_adapter.py`:
   - Required keys: `island, year, scenario, clean, CO235reduction,
     BAUCO2emissions, CO2_limit`.
   - Legal `scenario`: `base, grid, village, gridvillage, highimportprice,
     nocoal` (+ legacy `captive`/`gridcaptive`). Legal `clean`: `reference, clean`.
   - Optional passthrough: `mipgap, RE_limit, import_price,
     village_storage_max_mwh`.
   The semantics of each scenario flag live in the model's
   `functions/preflight.jl::scenario_settings`; read it if unsure.

2. **Map intent to the right lever, do not invent keys.**
   - "coordinate village + grid" → `scenario: gridvillage`; "standalone village"
     → `village`; "no coal" → `nocoal`; "carbon cap / clean target" →
     `clean: clean` (this is a **policy** lever — Tier C; set it deliberately).
   - "higher import price" → `import_price`; "bigger batteries allowed" →
     `village_storage_max_mwh`.
   - There is no free-form "X% solar" knob — penetration is an *outcome*. To bias
     it, adjust the relevant input CSVs or costs, and say so explicitly rather
     than pretending a config key exists.

3. **Always validate before emitting.** Call
   `VillageAdapter().validate_config(cfg)` (fast, Gurobi-free). For a stronger
   gate, run the model's own preflight:
   `julia --project=. run_model.jl --config <cfg> --preflight-only` in the
   model root. Do not hand a config to the runner until validation passes.

4. **For sweeps**, emit a list of configs (one per point) plus a one-line
   description of the swept axis. Keep every other key identical so downstream
   comparison is clean.

## Output

Write `config.json` (or a list) and report: the config, which lever each part of
the user's request maps to, and the validation result. Note any request element
you could **not** express as a config change (e.g. a target penetration).

## Example

User: "Build a standalone-village Maluku 2030 case and a coordinated one to compare."
→ two configs identical except `scenario: village` vs `scenario: gridvillage`;
both validate; hand both to `model-runner`; the pair is exactly the
coordination-saving comparison in the model's `docs/outputs_guide.md`.
