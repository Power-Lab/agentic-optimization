# Gurobi Parameters Reference

Key parameters used by the Model Executor when configuring Gurobi. All parameters can be passed via the `solver_hints` dict in `scenario_config.solver_settings`.

---

## 1. Optimality and Termination

| Parameter | Default | Recommended Range | Effect |
|---|---|---|---|
| `MIPGap` | 1e-4 | 1e-4 – 1e-2 | Relative gap tolerance; solver stops when `(incumbent - bound) / incumbent < MIPGap` |
| `MIPGapAbs` | 1e-10 | — | Absolute gap tolerance (rarely needed) |
| `TimeLimit` | `Inf` | 300–3600 | Wall-clock time limit in seconds |
| `SolutionLimit` | `Inf` | — | Stop after finding N feasible solutions |
| `BestObjStop` | `Inf` | — | Stop if incumbent ≤ this value |
| `BestBdStop` | `-Inf` | — | Stop if bound ≥ this value |

**For energy system UC problems:**
```python
model.set_param("MIPGap", 0.001)      # 0.1% — tight enough for production planning
model.set_param("TimeLimit", 600)     # 10 minutes per iteration
```

---

## 2. Numerical Stability

| Parameter | Default | When to Use | Effect |
|---|---|---|---|
| `NumericFocus` | 0 | Coefficient ratio > 1e6, or PRIMAL_DUAL_DISAGREEMENT | 0=speed, 1=balanced, 2=more careful, 3=most careful |
| `ScaleFlag` | -1 | Large coefficient range | -1=auto, 0=none, 1=geometric, 2=equilibrium, 3=auto + range |
| `ObjScale` | 0 | Objective values very large or very small | Scales objective to improve numerical properties; -0.5 = auto-geometric |
| `MarkowitzTol` | 0.0078 | Numerical issues in LP relaxation | Increases pivot stability (range: 1e-4 to 0.999) |

**When to apply:**
- Coefficient range > 1e6 → `NumericFocus = 2`, `ScaleFlag = 2`
- `PRIMAL_DUAL_DISAGREEMENT` → `NumericFocus = 3`, `ObjScale = -0.5`
- Objective value > 1e8 USD → `ObjScale = -0.5`

---

## 3. Presolve

| Parameter | Default | Effect |
|---|---|---|
| `Presolve` | -1 (auto) | -1=auto, 0=off, 1=conservative, 2=aggressive |
| `PreSparsify` | -1 (auto) | Reduces matrix density before solving |
| `PreCrush` | 1 | Translate solution back from presolved model |

**Note:** If presolve removes 0 rows/columns on a large model, this is suspicious — may indicate redundant constraints or an already-presolved model. Flag `NO_PRESOLVE_REDUCTION` and audit constraint formulation.

---

## 4. MIP Search Strategy

| Parameter | Default | Effect |
|---|---|---|
| `Cuts` | -1 (auto) | -1=auto, 0=off, 1=moderate, 2=aggressive, 3=very aggressive |
| `CutPasses` | -1 (auto) | Number of cutting plane passes at root node |
| `Heuristics` | 0.05 | Fraction of time spent on MIP heuristics [0,1] |
| `SubMIPNodes` | 500 | Nodes used in MIP heuristic sub-problems |
| `NodeMethod` | -1 (auto) | LP algorithm at tree nodes: -1=auto, 0=primal simplex, 1=dual simplex, 2=barrier |
| `BranchDir` | 0 | Branching direction: 0=auto, 1=up, -1=down |

**For slow convergence with loose LP bound:**
```python
model.set_param("Cuts", 2)         # Aggressive cutting planes
model.set_param("CutPasses", 5)    # 5 cutting plane passes at root
model.set_param("Heuristics", 0.1) # More heuristic effort
```

---

## 5. Warm Starting

| Parameter | Default | Effect |
|---|---|---|
| `Warmstart` | — | Load MIP start values from prior solution file |

**How to implement:**
```python
# Save current solution
model.write("solutions/run_id_001.sol")

# Load as warm start for next iteration
model.read("solutions/run_id_001.sol")
```

**Warm start eligibility:**
- Prior solution must be feasible
- Only parameter changes made (not structural — new generators, different horizon)
- Warm start can speed up convergence by 20–50% when the prior solution is close to optimal

---

## 6. Parallelism

| Parameter | Default | Effect |
|---|---|---|
| `Threads` | 0 (all cores) | Number of threads; 0=auto |
| `ConcurrentMIP` | 1 | Solve multiple MIPs in parallel and take best; set 2–4 on multi-socket |
| `DistributedMIPJobs` | 0 | Workers for distributed MIP (requires Gurobi cluster) |

**Recommended for HPC:**
```python
model.set_param("Threads", 8)        # Match --cpus-per-task in Slurm script
model.set_param("ConcurrentMIP", 2)  # Two parallel MIP solves (needs 16+ cores)
```

---

## 7. Output and Logging

| Parameter | Default | Effect |
|---|---|---|
| `LogFile` | `""` | Path to log file |
| `OutputFlag` | 1 | 1=print to stdout, 0=silent |
| `DisplayInterval` | 5 | Seconds between B&B progress display rows |
| `LogToConsole` | 1 | Mirror log to console |

**For the Model Executor:**
```python
model.set_param("LogFile", str(run_dir / "solver.log"))
model.set_param("DisplayInterval", 30)   # Match controller polling interval
```

---

## 8. Common Unit Commitment Problem Settings

Recommended baseline for a 24-hour UC with 4–8 generators and binary commitment:

```python
params = {
    "MIPGap": 0.001,
    "TimeLimit": 600,
    "Threads": 8,
    "Cuts": 1,
    "Presolve": 2,
    "NumericFocus": 0,
    "ScaleFlag": -1,
    "LogFile": "solver.log",
    "DisplayInterval": 30,
}
```

If numerical issues appear after the first run, escalate to:
```python
params.update({
    "NumericFocus": 3,
    "ScaleFlag": 2,
    "ObjScale": -0.5,
})
```
