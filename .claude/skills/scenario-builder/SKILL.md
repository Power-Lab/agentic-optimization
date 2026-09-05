---
name: scenario-builder
description: Turn a natural-language modeling goal into a validated optimization config/scenario for the active model adapter. Use when the user describes a scenario to run ("build a high-solar 2030 case", "add a carbon cap", "sweep a parameter"). Emits a config the model-runner skill can execute, gated by adapter validation. Model-agnostic — learns the schema from the adapter.
---

# Scenario Builder (Task 1)

Convert a research goal into a **validated config** for whatever model adapter is
active. You produce config; you do not run it — hand off to `model-runner`.

## Operating rules

1. **Resolve the active adapter; never hard-code a model.**
   ```python
   from framework import get_adapter
   adapter = get_adapter()              # AGENTIC_ADAPTER env, or the sole one
   print(adapter.describe_config())     # the model's schema, levers, semantics
   ```
   `describe_config()` is the source of truth for required keys, legal scenario
   values, and which levers are numeric vs parameter vs policy. Read it before
   proposing anything. (Whatever `get_adapter()` returns is the model under
   study; the same flow works for any registered adapter.)

2. **Map intent to the adapter's declared levers, do not invent keys.** Use only
   keys/values the adapter lists. If the user asks for something with no
   corresponding lever (e.g. a target renewable *penetration*, which is usually
   an outcome, not an input), say so explicitly rather than inventing a key.

3. **Mind the policy levers.** Anything `describe_config()` marks as a policy
   constraint (Tier C — e.g. a carbon cap or RE floor) changes the study's
   meaning. Set it deliberately and call it out; the refiner will not auto-touch
   it later.

4. **Always validate before emitting.**
   ```python
   result = adapter.validate_config(cfg)
   ```
   For a stronger gate, run the model's own preflight if it has one. Do not hand
   a config to the runner until validation passes.

5. **For sweeps**, emit a list of configs (one per point) plus a one-line
   description of the swept axis; keep every other key identical so downstream
   comparison is clean.

## Output

Write `config.json` (or a list) and report: the config, which adapter lever each
part of the request maps to, the validation result, and any request element you
could not express as a config change.
