# Julia / JuMP Patterns

## Model Layout

Use a predictable layout:

1. Load packages and config.
2. Normalize sets and parameters.
3. Build `Model`.
4. Attach optimizer and solver attributes.
5. Declare variables.
6. Declare named constraints.
7. Declare objective.
8. Optimize.
9. Export structured results.

Keep model construction in a function such as `build_model(config)::Model` so tests can build without solving.

## Gurobi Configuration

Prefer config-driven parameters:

```julia
set_optimizer_attribute(model, "TimeLimit", config["solver"]["time_limit_sec"])
set_optimizer_attribute(model, "MIPGap", config["solver"]["mip_gap"])
set_optimizer_attribute(model, "Threads", config["solver"]["threads"])
```

Use diagnostic parameters only for a reason:
- `InfUnbdInfo = 1` when diagnosing infeasible or unbounded models.
- `NumericFocus = 1` or higher only when logs show numerical trouble.
- `Presolve = 0` only for debugging, not as a default production setting.

## Constraint Patterns

Name constraint containers for traceability:

```julia
@constraint(model, power_balance[t in T],
    sum(gen[g, t] for g in G) + discharge[t] - charge[t] == demand[t]
)
```

Use consistent units across parameters. If demand is MW and time steps are hours, generation variables are usually MW and energy variables are MWh.

## Common Energy Model Constraints

- Power balance by time and region
- Generator upper and lower bounds
- Renewable availability limits
- Storage state of charge transition
- Storage charge/discharge power limits
- Reserve margin constraints
- Ramp up and ramp down constraints
- Emissions cap
- Minimum up/down time for unit commitment

## Debugging

When a model is infeasible:
- Confirm all indexed data exists for every set member and time period.
- Relax or isolate constraint families one at a time.
- Check demand, reserves, emissions caps, storage initial/final state, and generator availability.
- Export IIS artifacts when supported by the solver environment.

