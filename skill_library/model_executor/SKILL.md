---
name: model_executor
description: "Use this skill to execute a validated energy system scenario through the optimization model or mock dispatcher and collect raw solver outputs."
---

# Skill: Model Executor

## 1. Skill Name
**Model Executor**

---

## 2. Purpose
Translates a `scenario_config` into a concrete mathematical optimization model (unit commitment / economic dispatch), submits it to a Julia/JuMP/Gurobi solver (locally or on HPC), monitors execution, and retrieves raw solver outputs for downstream analysis.

In MVP / proof-of-concept mode, a mock dispatcher generates realistic synthetic dispatch and solver logs without invoking a real solver.

---

## 3. When to Use
Invoke this skill:
- When the Supervisory Controller issues `action = LAUNCH_SOLVER`
- On every iteration requiring a solver run (first run and all refined re-runs)
- Never invoke without a valid `scenario_config` and an approved `controller_decision`

---

## 4. Inputs

```yaml
executor_input:
  run_id: "run_id_001"
  scenario_config: { ... }            # Full output of Scenario Builder Agent
  solver_hints:
    mip_gap: 0.001
    time_limit_s: 600
    warm_start: false
    warm_start_solution_path: null    # path to prior .sol file if warm_start=true
  hpc_config:
    cluster: "local"                  # local | slurm | pbs
    partition: "compute"
    nodes: 1
    cpus_per_task: 8
    memory_GB: 32
    walltime_h: 2
    julia_threads: 8
    gurobi_license_server: "localhost:41954"
  model_formulation:
    type: "unit_commitment"           # unit_commitment | economic_dispatch | dc_opf
    binary_commitment: true           # true = MIP, false = LP relaxation
    network_model: "single_bus"
  mode: "mock"                        # mock | julia | slurm
  inject_issue: null                  # null | infeasible | timeout | numerical (mock only)
```

---

## 5. Outputs

```yaml
executor_output:
  run_id: "run_id_001"
  job_id: "local_pid_14823"           # Slurm job ID or local PID
  status: "COMPLETED"                 # SUBMITTED | RUNNING | COMPLETED | TIMEOUT | FAILED | INFEASIBLE
  solver_termination_status: "OPTIMAL"  # OPTIMAL | INFEASIBLE | TIME_LIMIT | NUMERICAL_ERROR
  objective_value_USD: 163249.0
  mip_gap_final: 0.00082
  solve_time_s: 312.7
  wall_time_s: 328.4
  solution:
    dispatch_MW:
      gas_cc_1:  [180, 180, 180, 220, 310, 420, 530, 560, 562, 544, 505, 460, 430, 408, 400, 430, 500, 560, 530, 460, 370, 270, 200, 180]
      gas_ct_1:  [0,   0,   0,   0,   0,   0,   0,   0,   40,  40,  40,  40,  40,  40,  40,  40,  40,  40,  40,  40,  0,   0,   0,   0  ]
      solar_1:   [0,   0,   0,   0,   58,  244, 487, 608, 618, 589, 553, 495, 456, 430, 396, 330, 208, 82,  13,  0,   0,   0,   0,   0  ]
      wind_1:    [297, 314, 323, 305, 288, 300, 327, 330, 336, 315, 290, 272, 253, 237, 249, 270, 283, 288, 276, 257, 243, 237, 228, 219]
    commitment_status:
      gas_cc_1:  [1,1,1,1,1,1,1,1,1,1,1,1,1,1,1,1,1,1,1,1,1,1,1,1]
      gas_ct_1:  [0,0,0,0,0,0,0,0,1,1,1,1,1,1,1,1,1,1,1,1,0,0,0,0]
    load_shedding_MW: [0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0]
    total_emissions_tCO2: 2752.0
    renewable_fraction: 0.622
  log_file_path: "runs/run_id_001/solver.log"
  solution_file_path: "runs/run_id_001/dispatch.csv"
  model_file_path: "runs/run_id_001/model.jl"
```

---

## 6. Step-by-Step Instructions

### Mock Mode (MVP)

1. **Generate synthetic demand and VRE profiles** from `scenario_config` demand/renewable assumptions.
2. **Merit-order dispatch**: renewables first (zero cost), then thermal in cost order.
3. **Two-pass ramp clipping**: forward-clip thermal dispatch to stay within `ramp_up/down_MW_h` limits.
4. **Balance pass**: any under-supply becomes load shedding; any over-supply curtails VRE proportionally.
5. **Write `solver.log`** with realistic Gurobi-style B&B progress table (or failure message if `inject_issue` set).
6. **Write `dispatch.csv`** with columns `[timestep, demand_MW, <gen_ids>, load_shedding_MW]`.
7. **Compute and write `output_summary.json`**: objective, emissions, renewable fraction, status.

### Real Solver Mode (Julia/JuMP)

1. **Generate the JuMP model file** (`model.jl`):
   - Decision variables: `p[g,t]` (MW), `u[g,t]` (binary commitment), `v[g,t]` (startup), `w[g,t]` (shutdown), `s[t]` (load shedding), `c[g,t]` (curtailment).
   - Objective: `min sum_t sum_g [cost_linear*p[g,t] + cost_fixed*u[g,t] + cost_startup*v[g,t]] + penalty_shed * sum_t s[t]`
   - Constraints: power balance, generator bounds, ramp up/down, min up/down time, spinning reserve, emission cap (if set), renewable penetration (if set).

2. **Apply solver settings**: `MIPGap`, `TimeLimit`, `Threads`. Load warm start if `warm_start=true`.

3. **Submit job**:
   - Slurm: generate job script, `sbatch`, capture `job_id`.
   - Local: spawn Julia process, capture PID.

4. **Monitor**: poll every 30 s. Relay `solver_monitor` feed to Supervisory Controller.

5. **Retrieve results**: parse Gurobi output; extract primal solution, duals (marginal prices), derived quantities.

6. **Emit `executor_output`** with all solution fields. Set `solution = null` if infeasible or crashed.

---

## 7. Heuristics / Rules

- **Load shedding penalty**: Always ≥ 10× the highest `cost_linear_USD_per_MWh` in the fleet. Default: `$10,000/MWh`.
- **LP relaxation fallback**: If MIP is infeasible, automatically attempt LP relaxation and tag output `WARN: LP_RELAXATION_USED`.
- **Numerical scaling**: If any `capacity_MW` differs from another by > 3 orders of magnitude, set `ScaleFlag = 2`.
- **Marginal prices**: Label as `approximate` when binary commitment was used (duals are only exact for LP).
- **Artifact retention**: Always save `.jl` model file and `solver.log`. These are required by downstream analyzers.
- **Mock mode realism**: Mock output must satisfy power balance exactly (within 0.5 MW). The output analyzer will flag violations.

---

## 8. Failure Modes

| Failure | Condition | Response |
|---|---|---|
| `JOB_SUBMISSION_FAILED` | HPC unreachable or license server down | Retry after 60 s. Fall back to local mode. |
| `LICENSE_ERROR` | Gurobi license checkout fails | Wait 5 min, retry. Switch to HiGHS if still failing. |
| `MODEL_BUILD_ERROR` | JuMP throws during model construction | Log stack trace. Route to Refiner Agent with `MODEL_BUILD_ERROR`. |
| `SOLVER_INFEASIBLE` | Gurobi returns INFEASIBLE | `solver_termination_status = INFEASIBLE`. Pass log to Solver Log Analyzer. |
| `SOLVER_TIMEOUT` | Time limit reached | Return best incumbent. Flag MIP gap. |
| `SOLUTION_PARSE_ERROR` | Output files malformed | Retry parse. If fails, `solution = null`, route to Solver Log Analyzer. |

---

## 9. Example

**Mock run output summary (`output_summary.json` excerpt):**
```json
{
  "run_id": "run_id_001",
  "status": "OPTIMAL",
  "solver_termination_status": "OPTIMAL",
  "objective_value_USD": 163249.0,
  "mip_gap_final": 0.00082,
  "solve_time_s": 312.7,
  "total_emissions_tCO2": 2752.0,
  "renewable_fraction": 0.622,
  "total_demand_MWh": 18744.0,
  "emission_cap_tCO2": 5000,
  "renewable_penetration_min": 0.6,
  "renewable_target_met": true,
  "emission_cap_met": true
}
```

**Extension points (real solver hooks):**
```python
# === REAL SOLVER HOOK ===
# Uncomment and implement ONE of:

# def run_julia_local(config, run_dir):
#     cmd = ["julia", "--threads=auto", "model/unit_commitment.jl",
#            "--config", str(run_dir/"scenario_config.yaml"),
#            "--output-dir", str(run_dir)]
#     subprocess.Popen(cmd, stdout=open(run_dir/"solver.log","w"))

# def submit_slurm_job(config, run_dir):
#     job_script = _generate_slurm_script(config, run_dir)
#     result = subprocess.run(["sbatch", job_script], capture_output=True, text=True)
#     return result.stdout.strip().split()[-1]   # job_id
```
