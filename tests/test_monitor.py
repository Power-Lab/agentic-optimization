"""Live run monitoring tests — solver-free.

Covers ``framework.process.stream_command`` (streamed tee, abort, timeout,
ABORT file, ticks), ``framework.monitor`` (``LogWatcher`` on real and
synthesised solver logs, stall / time-limit-near detection, ``RunMonitor``
fan-out and persistence, ``replay_log``), the runner wiring
(``run_and_record(..., on_event=...)`` for streaming and legacy adapters,
``Execution.monitor`` round-trip) and ``python -m framework.watch``.

Real logs: ``runs/smoke_base_maluku/solver.log`` (Gurobi MILP, OPTIMAL),
``runs/demo_infeasible_co2/solver.log`` (Gurobi infeasible + Julia traceback)
and ``runs/demo_garuda_co2_floor/solver.log`` (HiGHS 1.14 LP, infeasible) are
used when present (``runs/`` is not committed) — the same dialects are also
covered by the embedded excerpts below, so nothing depends on them.
"""

from __future__ import annotations

import io
import json
import sys
import threading
import time
from pathlib import Path

import pytest

from framework import watch as watch_mod
from framework.adapter import Adapter, ValidationResult
from framework.interventions import InterventionSpec
from framework.monitor import (
    EVENT_KINDS,
    NOTABLE_KINDS,
    TERMINAL_STATUSES,
    LogWatcher,
    RunEvent,
    RunMonitor,
    normalize_status,
    replay_log,
    status_from_marker,
)
from framework.process import StreamResult, python_child, request_abort, stream_command
from framework.run_record import Execution, RunRecord
from framework.runner import adapter_accepts_on_event, attach_monitor_summary, run_and_record

REPO = Path(__file__).resolve().parents[1]
RUNS = REPO / "runs"
REAL_GUROBI_MILP = RUNS / "smoke_base_maluku" / "solver.log"
REAL_GUROBI_INFEASIBLE = RUNS / "demo_infeasible_co2" / "solver.log"
REAL_HIGHS_LP_INFEASIBLE = RUNS / "demo_garuda_co2_floor" / "solver.log"


# ==========================================================================
# log samples
# ==========================================================================

# Excerpt of the real Gurobi 13 MILP log (branch-and-bound rows thinned out).
GUROBI_MILP = """\
Set parameter Username
Academic license - for non-commercial use only - expires 2027-03-13
Set parameter MIPGap to value 0.01
Set parameter Crossover to value 0
Set parameter TimeLimit to value 259200
Gurobi Optimizer version 13.0.0 build v13.0.0rc1 (mac64[arm] - Darwin 25.5.0 25F80)

CPU model: Apple M3 Max
Thread count: 14 physical cores, 14 logical processors, using up to 14 threads

Non-default parameters:
TimeLimit  259200
MIPGap  0.01
Crossover  0

Optimize a model with 799700 rows, 320019 columns and 2512814 nonzeros (Min)
Model fingerprint: 0xa9c69711
Model has 153352 linear objective coefficients
Variable types: 106323 continuous, 213696 integer (213696 binary)
Coefficient statistics:
  Matrix range     [1e-03, 2e+02]
  Objective range  [1e-01, 3e+06]
  Bounds range     [6e-01, 3e+03]
  RHS range        [4e-01, 2e+02]
Found heuristic solution: objective 3.289510e+09
Presolve removed 158533 rows and 4882 columns
Presolve time: 4.38s
Presolved: 641167 rows, 315137 columns, 2236826 nonzeros
Variable types: 101402 continuous, 213735 integer (213731 binary)

Deterministic concurrent LP optimizer: primal simplex, dual simplex, and barrier
Showing barrier log only...

Warning: Concurrent optimizer requires crossover - forcing it on
Root barrier log...

Ordering time: 0.20s

Barrier statistics:
 Dense cols : 43
 AA' NZ     : 2.489e+06
 Factor NZ  : 8.792e+06 (roughly 260 MB of memory)
 Factor Ops : 1.163e+09 (less than 1 second per iteration)
 Threads    : 12

                  Objective                Residual
Iter       Primal          Dual         Primal    Dual     Compl     Time
   0   1.81855777e+15 -4.47009366e+15  9.34e+06 6.70e+04  1.43e+13     6s
   1   1.66372141e+15 -1.68314424e+15  6.61e+06 8.25e+06  8.74e+12     6s
  74   8.61069900e+07  8.61069900e+07  1.16e-09 1.63e-05  1.01e-08    13s
  75   8.61069900e+07  8.61069900e+07  1.24e-11 1.63e-05  2.51e-13    13s

Barrier solved model in 75 iterations and 12.81 seconds (36.47 work units)
Optimal objective 8.61069900e+07


Root crossover log...

  236834 DPushes remaining with DInf 0.0000000e+00                13s
       0 DPushes remaining with DInf 0.0000000e+00                13s

    2438 PPushes remaining with PInf 0.0000000e+00                13s
       0 PPushes remaining with PInf 0.0000000e+00                13s

  Push phase complete: Pinf 0.0000000e+00, Dinf 1.5388454e-06     13s


Root simplex log...

Iteration    Objective       Primal Inf.    Dual Inf.      Time
  119378    8.6106990e+07   0.000000e+00   0.000000e+00     13s
Crossover time: 0.62 seconds (1.16 work units)
  119378    8.6106990e+07   0.000000e+00   0.000000e+00     13s
Concurrent spin time: 3.10s (can be avoided by choosing Method=3)

Solved with barrier

Root relaxation: objective 8.610699e+07, 132737 iterations, 12.56 seconds (21.19 work units)

    Nodes    |    Current Node    |     Objective Bounds      |     Work
 Expl Unexpl |  Obj  Depth IntInf | Incumbent    BestBd   Gap | It/Node Time

     0     0 8.6107e+07    0 5081 3.2895e+09 8.6107e+07  97.4%     -   19s
H    0     0                    2.179905e+08 8.6107e+07  60.5%     -   20s
H    0     0                    9.138743e+07 8.6107e+07  5.78%     -   21s
     0     0 8.6449e+07    0 3973 9.1387e+07 8.6449e+07  5.40%     -   26s
     0     0 8.7106e+07    0 5368 9.1387e+07 8.7106e+07  4.68%     -   54s
H    0     0                    9.125678e+07 8.7210e+07  4.43%     -   64s
     0     0 8.7248e+07    0 8232 9.1246e+07 8.7248e+07  4.38%     -   74s
H    0     0                    8.959888e+07 8.7248e+07  2.62%     -   75s
H    0     0                    8.867986e+07 8.7261e+07  1.60%     -   88s
H    0     0                    8.835479e+07 8.7262e+07  1.24%     -   97s
     0     0 8.7312e+07    0 13796 8.8340e+07 8.7312e+07  1.16%     -  168s
H    0     0                    8.778610e+07 8.7312e+07  0.54%     -  170s

Cutting planes:
  Cover: 48
  MIR: 719

Explored 1 nodes (420819 simplex iterations) in 170.40 seconds (611.87 work units)
Thread count was 14 (of 14 available processors)

Solution count 10: 8.77861e+07 8.83403e+07 8.83541e+07 ... 3.28951e+09

Optimal solution found (tolerance 1.00e-02)
Best objective 8.778609672942e+07, best bound 8.731172667084e+07, gap 0.5404%

User-callback calls 38819, time in user-callback 0.01 sec
The model solved successfully.

[stderr]
  Activating project at `~/models/x`
"""

# Real Gurobi infeasible-or-unbounded verdict followed by the Julia traceback the
# model wrapper throws when it reads the objective anyway (frames thinned).
GUROBI_INFEASIBLE_JULIA = """\
Set parameter TimeLimit to value 259200
Gurobi Optimizer version 13.0.0 build v13.0.0rc1 (mac64[arm] - Darwin 25.5.0 25F80)

Optimize a model with 799702 rows, 320019 columns and 2715758 nonzeros (Min)
Model fingerprint: 0x499252ba
Variable types: 106323 continuous, 213696 integer (213696 binary)
Presolve removed 39001 rows and 70 columns
Presolve time: 0.06s

Explored 0 nodes (0 simplex iterations) in 0.18 seconds (0.59 work units)
Thread count was 1 (of 14 available processors)

Solution count 0

Model is infeasible or unbounded
Best objective -, best bound -, gap -

User-callback calls 60, time in user-callback 0.00 sec
The model did not solve successfully. Termination status: INFEASIBLE_OR_UNBOUNDED

[stderr]
  Activating project at `~/models/x`
ERROR: LoadError: Result index of attribute MathOptInterface.ObjectiveValue(1) out of bounds. There are currently 0 solution(s) in the model.
Stacktrace:
  [1] check_result_index_bounds
    @ ~/.julia/packages/MathOptInterface/Q3V1z/src/attributes.jl:238 [inlined]
  [2] get(model::Gurobi.Optimizer, attr::MathOptInterface.ObjectiveValue)
    @ Gurobi ~/.julia/packages/Gurobi/K2XSK/src/MOI_wrapper/MOI_wrapper.jl:3301
  [3] objective_value(model::Model; result::Int64)
    @ JuMP ~/.julia/packages/JuMP/0tD10/src/objective.jl:122
  [4] top-level scope
    @ ~/models/x/run_model.jl:56
in expression starting at /home/me/models/x/run_model.jl:56
"""

# A Gurobi MIP that hits its time limit (time limit learned from the log).
GUROBI_MIP_TIME_LIMIT = """\
Set parameter TimeLimit to value 100
Gurobi Optimizer version 10.0.3 build v10.0.3rc0 (linux64)
Optimize a model with 799700 rows, 320019 columns and 2512814 nonzeros (Min)
Variable types: 106323 continuous, 213696 integer (213696 binary)
Found heuristic solution: objective 3.289510e+09
Presolve removed 158533 rows and 4882 columns
Presolve time: 4.38s
Presolved: 641167 rows, 315137 columns, 2236826 nonzeros
Root relaxation: objective 8.610699e+07, 132737 iterations, 12.56 seconds (21.19 work units)

    Nodes    |    Current Node    |     Objective Bounds      |     Work
 Expl Unexpl |  Obj  Depth IntInf | Incumbent    BestBd   Gap | It/Node Time

     0     0 8.6107e+07    0 5081 3.2895e+09 8.6107e+07  97.4%     -   19s
H    0     0                    9.138743e+07 8.6107e+07  5.78%     -   21s
     0     0 8.6449e+07    0 3973 9.1387e+07 8.6449e+07  5.40%     -   26s
     0     2 8.6449e+07    0 3973 9.1387e+07 8.6449e+07  5.40%     -   40s
    10     8 8.6449e+07    3 3811 9.1387e+07 8.6449e+07  5.40%  1204   70s
    31    20 8.6449e+07    5 3702 9.1387e+07 8.6449e+07  5.40%   988   92s

Explored 45 nodes (512345 simplex iterations) in 100.02 seconds (300.00 work units)
Thread count was 14 (of 14 available processors)

Solution count 3: 9.13874e+07 2.1799e+08 3.28951e+09

Time limit reached
Best objective 9.138743012345e+07, best bound 8.644900012345e+07, gap 5.4034%
Capacity expansion reached the time limit (expansion).
"""

# A pure LP solved by Gurobi's barrier from a Python driver (marker lines).
GUROBI_LP_BARRIER = """\
Set parameter Method to value 2
Set parameter Crossover to value 0
Gurobi Optimizer version 10.0.3 build v10.0.3rc0 (linux64)

Optimize a model with 452113 rows, 388902 columns and 1837720 nonzeros
Model fingerprint: 0x1234abcd
Coefficient statistics:
  Matrix range     [1e-04, 1e+04]
Presolve removed 100123 rows and 90011 columns
Presolve time: 1.23s
Presolved: 351990 rows, 298891 columns, 1650000 nonzeros

Barrier statistics:
 Dense cols : 12
 Threads    : 32

                  Objective                Residual
Iter       Primal          Dual         Primal    Dual     Compl     Time
   0   1.23456789e+12 -4.56789012e+12  9.34e+06 6.70e+04  1.43e+13     3s
   1   9.87654321e+11 -2.34567890e+12  6.61e+06 8.25e+06  8.74e+12     3s
  30   3.10203055e+09  3.10203040e+09  1.20e-08 1.60e-05  2.50e-06    28s
  31   3.10203050e+09  3.10203050e+09  1.24e-11 1.63e-05  2.51e-13    30s

Barrier solved model in 31 iterations and 30.12 seconds (36.47 work units)
Optimal objective 3.10203050e+09
[driver] stage: postprocess
[driver] status: OPTIMAL
"""

# Gurobi LP infeasible + the Python traceback a status-blind driver raises.
GUROBI_LP_INFEASIBLE_PYTHON = """\
Gurobi Optimizer version 10.0.3 build v10.0.3rc0 (linux64)
Optimize a model with 452113 rows, 388902 columns and 1837720 nonzeros
Presolve removed 100123 rows and 90011 columns
Presolve time: 1.23s

Solved in 0 iterations and 0.01 seconds (0.00 work units)
Infeasible model
Traceback (most recent call last):
  File "/work/driver.py", line 210, in <module>
    main()
  File "/work/driver.py", line 180, in main
    obj = model.objVal
  File "src/gurobipy/model.pxi", line 372, in gurobipy.Model.__getattr__
  File "src/gurobipy/model.pxi", line 1892, in gurobipy.Model.getAttr
gurobipy.GurobiError: Unable to retrieve attribute 'objVal'
[driver] status: INFEASIBLE error_origin: solver
"""

# Synthesised from the documented HiGHS 1.7 MIP log format (Src column, B&B
# table, "Solving report" block, source legend).
HIGHS_MIP_OPTIMAL = """\
Running HiGHS 1.7.2 (git hash: 5ce7a2753): Copyright (c) 2024 HiGHS under MIT licence terms
Coefficient ranges:
  Matrix [1e-03, 2e+02]
  Cost   [1e-01, 3e+06]
  Bound  [6e-01, 3e+03]
  RHS    [4e-01, 2e+02]
Presolving model
641167 rows, 315137 cols, 2236826 nonzeros  4s
612040 rows, 301220 cols, 2101533 nonzeros  9s

Solving MIP model with:
   612040 rows
   301220 cols (201118 binary, 4 integer, 0 implied int., 100098 continuous)
   2101533 nonzeros

        Nodes      |    B&B Tree     |            Objective Bounds              |  Dynamic Constraints |       Work
Src  Proc. InQueue |  Leaves   Expl. | BestBound       BestSol              Gap |   Cuts   InLp Confl. | LpIters     Time

         0       0         0   0.00%   -inf            inf                  inf        0      0      0         0    12.3s
 R       0       0         0   0.00%   8.610699e+07    3.289510e+09    3720.05%        0      0      0    132737    19.4s
 H       0       0         0   0.00%   8.610699e+07    2.179905e+08     153.16%        0      0      0    132737    20.1s
 L       0       0         0   0.00%   8.644900e+07    9.138743e+07       5.71%     1288    354      0    189044    41.7s
         0       0         0   0.00%   8.721000e+07    9.138743e+07       4.79%     2231    482      0    240113    64.0s
 T       0       0         0   0.00%   8.725400e+07    9.125678e+07       4.59%     2231    482      0    250087    66.2s
 L       0       0         0   0.00%   8.726100e+07    8.867986e+07       1.63%     2903    511      0    301240    88.5s
 B       1       0         1 100.00%   8.731173e+07    8.778610e+07       0.54%     3105    533      0    420819   170.4s

Solving report
  Status            Optimal
  Primal bound      87786096.7294
  Dual bound        87311726.6708
  Gap               0.54% (tolerance: 1%)
  Solution status   feasible
                    87786096.7294 (objective)
                    0 (bound viol.)
                    9.6e-13 (int. viol.)
                    0 (row viol.)
  Timing            170.40 (total)
                    4.38 (presolve)
                    0.00 (postsolve)
  Nodes             1
  LP iterations     420819 (total)
                    0 (strong br.)
                    132737 (separation)
                    0 (heuristics)
Src: B => Branching; C => Central rounding; F => Feasibility pump; H => Heuristic; L => Sub-MIP;
     P => Empty MIP; R => Randomized rounding; S => Solve LP; T => Evaluate node; U => Unbounded;
     z => Trivial zero; l => Trivial lower; u => Trivial upper; p => Trivial point; X => User solution
Capacity expansion solved successfully (expansion).
"""

HIGHS_MIP_TIME_LIMIT = """\
Running HiGHS 1.7.2 (git hash: 5ce7a2753): Copyright (c) 2024 HiGHS under MIT licence terms
Presolving model
641167 rows, 315137 cols, 2236826 nonzeros  4s

Solving MIP model with:
   612040 rows
   301220 cols (201118 binary, 4 integer, 0 implied int., 100098 continuous)
   2101533 nonzeros

        Nodes      |    B&B Tree     |            Objective Bounds              |  Dynamic Constraints |       Work
Src  Proc. InQueue |  Leaves   Expl. | BestBound       BestSol              Gap |   Cuts   InLp Confl. | LpIters     Time

         0       0         0   0.00%   -inf            inf                  inf        0      0      0         0    12.3s
 R       0       0         0   0.00%   8.610699e+07    3.289510e+09    3720.05%        0      0      0    132737    19.4s
 L       0       0         0   0.00%   8.644900e+07    9.138743e+07       5.71%     1288    354      0    189044    41.7s
        12       5         3   1.20%   8.644900e+07    9.138743e+07       5.71%     2231    482      0    240113   300.0s
        40      11         9   4.80%   8.644900e+07    9.138743e+07       5.71%     2231    482      0    290113   600.0s
        88      20        15   9.10%   8.644900e+07    9.138743e+07       5.71%     2231    482      0    340113   900.0s

Solving report
  Status            Time limit reached
  Primal bound      91387430.12
  Dual bound        86449000
  Gap               5.71% (tolerance: 1%)
  Solution status   feasible
                    91387430.12 (objective)
  Timing            900.02 (total)
                    4.38 (presolve)
                    0.00 (postsolve)
  Nodes             88
  LP iterations     340113 (total)
Capacity expansion reached the time limit (expansion).
"""

HIGHS_MIP_INFEASIBLE = """\
Running HiGHS 1.7.2 (git hash: 5ce7a2753): Copyright (c) 2024 HiGHS under MIT licence terms
Coefficient ranges:
  Matrix [1e-03, 2e+02]
Presolving model
Presolve: Infeasible

Solving report
  Status            Infeasible
  Primal bound      inf
  Dual bound        inf
  Gap               inf
  Solution status   -
  Timing            0.03 (total)
                    0.03 (presolve)
                    0.00 (postsolve)
  Nodes             0
  LP iterations     0 (total)
Capacity expansion is infeasible.
"""

# HiGHS dual simplex on an LP (what a dispatch-only engine prints).
HIGHS_LP_OPTIMAL = """\
Running HiGHS 1.7.2 (git hash: 5ce7a2753): Copyright (c) 2024 HiGHS under MIT licence terms
LP has 801100 rows; 320020 cols; 2674308 nonzeros
Coefficient ranges:
  Matrix  [6e-07, 2e+02]
  Cost    [1e-01, 3e+06]
Presolving model
612040 rows, 301220 cols, 2101533 nonzeros  2s
Presolve : Reductions: rows 612040(-189060); columns 301220(-18800); elements 2101533(-572775)
Solving the presolved LP
Using EKK dual simplex solver - serial
  Iteration        Objective     Infeasibilities num(sum)
          0    -2.4711749787e-04 Pr: 1388(61986.2); Du: 0(2.1604e-12) 1.1s
      20000     2.1030050000e+02 Pr: 240(1234.5) 8.0s
      48090     4.1875012738e+02 Pr: 0(0) 15.2s
Solving the original LP from the solution after postsolve
Model   status      : Optimal
Simplex   iterations: 48090
Objective value     :  4.1875012738e+02
HiGHS run time      :         15.44
Dispatch solved successfully (relaxed unit commitment).
"""

# Verbatim from the real HiGHS 1.14 log of an infeasible dispatch LP.
HIGHS_LP_INFEASIBLE_114 = """\
  Activating project at `~/models/x`
Running HiGHS 1.14.0 (git hash: 7df0786de3): Copyright (c) 2026 under Apache 2.0 license terms
Using BLAS: blastrampoline
LP has 801100 rows; 320020 cols; 2674308 nonzeros
Coefficient ranges:
  Matrix  [6e-07, 2e+02]
  Cost    [1e-01, 3e+06]
  Bound   [6e-01, 3e+03]
  RHS     [4e-01, 2e+02]
WARNING: Problem has some excessively large costs
WARNING:    Consider scaling the objective by 1e-1, or setting the user_objective_scale option to -2
Presolving model
Problem status detected on presolve: Infeasible
Model status        : Infeasible
Objective value     :  0.0000000000e+00
HiGHS run time      :          0.67
Solving LP to try to compute dual ray
LP has 801100 rows; 320020 cols; 2674308 nonzeros
Solving LP without presolve or useful basis
Using dual simplex solver
  Iteration        Objective     Infeasibilities num(sum)
          0    -2.4711749787e-04 Pr: 1388(61986.2); Du: 0(2.1604e-12) 1.1s
      48090     3.4655074496e-02 3.9s

Model status        : Infeasible
Simplex   iterations: 48090
Objective value     :  0.0000000000e+00
P-D objective error :  3.3494326129e-02
HiGHS run time      :          3.91
Solving linear system to compute dual ray
Dispatch is infeasible (fleet cannot meet demand even with full non-served energy).
"""

# What a linopy-driven HiGHS solve prints around the solver log.
LINOPY_WRAPPER = """\
INFO:linopy.model: Solve problem using Highs solver
INFO:linopy.io: Writing time: 0.12s
Running HiGHS 1.7.2 (git hash: 5ce7a2753): Copyright (c) 2024 HiGHS under MIT licence terms
LP has 2016 rows; 1344 cols; 6048 nonzeros
Presolving model
Model   status      : Optimal
Objective value     :  1.2345678900e+06
HiGHS run time      :          0.05
INFO:linopy.constants: Optimization successful:
Status: ok
Termination condition: optimal
Solution: 1344 primals, 2016 duals
Objective: 1.23e+06
Solver model: available
Solver message: Optimal
[toy] stage: write_outputs
[toy] status: OPTIMAL
"""


# ==========================================================================
# helpers
# ==========================================================================

def feed(text: str, **kw) -> tuple:
    """Feed ``text`` through a fresh watcher; return (watcher, events)."""
    kw.setdefault("stall_seconds", None)
    w = LogWatcher(**kw)
    events = w.feed_many(text.splitlines(keepends=True))
    events.extend(w.finish())
    return w, events


def kinds(events) -> list:
    return [e.kind for e in events]


def of_kind(events, kind: str) -> list:
    return [e for e in events if e.kind == kind]


def write_log(run_dir: Path, text: str) -> Path:
    run_dir.mkdir(parents=True, exist_ok=True)
    path = run_dir / "solver.log"
    path.write_text(text)
    return path


# ==========================================================================
# vocabulary
# ==========================================================================

def test_event_kinds_and_terminal_statuses_are_declared():
    for kind in ("progress", "incumbent", "stall", "time_limit_near", "status",
                 "solver_status", "traceback", "warning", "preflight"):
        assert kind in EVENT_KINDS
    assert NOTABLE_KINDS <= set(EVENT_KINDS)
    assert {"OPTIMAL", "INFEASIBLE", "TIME_LIMIT", "ERROR"} <= TERMINAL_STATUSES


@pytest.mark.parametrize("token,expected", [
    ("Optimal", "OPTIMAL"), ("optimal", "OPTIMAL"), ("LOCALLY_SOLVED", "OPTIMAL"),
    ("INFEASIBLE_OR_UNBOUNDED", "INFEASIBLE"), ("Infeasible", "INFEASIBLE"),
    ("Unbounded", "UNBOUNDED"), ("Time limit reached", "TIME_LIMIT"),
    ("TIME_LIMIT", "TIME_LIMIT"), ("Iteration limit reached", "LIMIT"),
    ("NUMERICAL_ERROR", "ERROR"), ("Unknown", "ERROR"), ("interrupted", "INTERRUPTED"),
    ("ok", "OK"), ("", None),
])
def test_normalize_status(token, expected):
    assert normalize_status(token) == expected


@pytest.mark.parametrize("line,expected", [
    ("Capacity expansion solved successfully (expansion).", "OPTIMAL"),
    ("Dispatch solved successfully (relaxed unit commitment).", "OPTIMAL"),
    ("The model solved successfully.", "OPTIMAL"),
    ("Capacity expansion reached the time limit (expansion).", "TIME_LIMIT"),
    ("Dispatch reached the time limit.", "TIME_LIMIT"),
    ("Capacity expansion is infeasible.", "INFEASIBLE"),
    ("Dispatch is infeasible (fleet cannot meet demand).", "INFEASIBLE"),
    ("Capacity expansion did not solve. Termination status: NUMERICAL_ERROR", "ERROR"),
    ("The model did not solve successfully. Termination status: INFEASIBLE_OR_UNBOUNDED",
     "INFEASIBLE"),
    ("[toy] status: OPTIMAL", "OPTIMAL"),
    ("[toy] status: TIME_LIMIT", "TIME_LIMIT"),
    ("[toy] status: ERROR error_origin: runtime", "ERROR"),
    ("Termination condition: infeasible", "INFEASIBLE"),
    ("Status: ok", None),                       # informational, never terminal
    ("INFO:linopy.constants: Optimization successful:", None),
    ("The model was not solved successfully.", None),  # negated: no false OPTIMAL
    ("Solution count 0", None),
])
def test_status_from_marker(line, expected):
    assert status_from_marker(line) == expected


# ==========================================================================
# LogWatcher on the Gurobi dialect
# ==========================================================================

def test_gurobi_milp_excerpt():
    w, events = feed(GUROBI_MILP)
    s = w.snapshot()
    assert (s["solver"], s["solver_version"]) == ("gurobi", "13.0.0")
    assert s["is_mip"] is True
    assert s["time_limit"] == 259200.0            # learned from "Set parameter TimeLimit"
    assert s["model_size"]["rows"] == 799700 and s["model_size"]["sense"] == "Min"
    assert s["model_size"]["presolved"]["rows"] == 641167
    assert s["model_size"]["binary"] == 213731
    assert len(of_kind(events, "barrier_iter")) == 4
    root = of_kind(events, "root_relaxation")
    assert root and root[0].data["objective"] == pytest.approx(8.610699e7)
    assert s["lp_objective"] == pytest.approx(8.610699e7)
    # the heuristic line + 7 "H" rows are the incumbents; plain rows are progress
    assert len(of_kind(events, "incumbent")) == 8
    assert s["n_incumbents"] == 8
    assert len(of_kind(events, "progress")) == 5
    assert s["best_obj"] == pytest.approx(87786096.72942)
    assert s["bound"] == pytest.approx(87311726.67084)
    assert s["last_gap"] == pytest.approx(0.005404)
    assert s["gap_pct"] == pytest.approx(0.5404)
    assert s["nodes"] == 1
    assert s["solver_time_s"] == pytest.approx(170.4)
    assert of_kind(events, "explored")[0].data["iterations"] == 420819
    # solver verdict then the wrapper's marker; the marker wins as source
    verdicts = of_kind(events, "solver_status")
    assert verdicts and verdicts[-1].data["status"] == "OPTIMAL"
    assert verdicts[-1].data["tolerance"] == pytest.approx(0.01)
    assert (s["status"], s["status_source"]) == ("OPTIMAL", "marker")
    assert s["phase"] == "finished" and s["finished"] is True
    assert s["n_warnings"] == 1 and "crossover" in s["warnings"][0]
    assert s["n_errors"] == 0 and s["traceback"] is None
    # the licence banner and "[stderr]" are not errors
    assert not of_kind(events, "error")
    # phases visited in order
    phases = [e.data["phase"] for e in of_kind(events, "phase")]
    assert phases.index("presolve") < phases.index("barrier") < phases.index("branch_and_bound")
    assert phases[-1] == "finished"
    # solution event carries the final bounds
    final = [e for e in of_kind(events, "solution") if e.data.get("final")]
    assert final[-1].data["gap"] == pytest.approx(0.005404)


def test_gurobi_infeasible_with_julia_traceback():
    w, events = feed(GUROBI_INFEASIBLE_JULIA)
    s = w.snapshot()
    assert of_kind(events, "solver_status")[0].data["status"] == "INFEASIBLE"
    assert (s["status"], s["status_source"]) == ("INFEASIBLE", "marker")
    assert s["best_obj"] is None and s["bound"] is None and s["last_gap"] is None
    assert s["nodes"] == 0
    err = of_kind(events, "error")
    assert err and err[0].data["language"] == "julia"
    tb = of_kind(events, "traceback")
    assert len(tb) == 1
    assert tb[0].data["language"] == "julia"
    assert tb[0].data["n_frames"] == 4
    assert tb[0].data["frames"][0]["func"] == "check_result_index_bounds"
    assert tb[0].data["frames"][0]["at"].endswith("attributes.jl:238 [inlined]")
    assert s["traceback"]["at"].endswith("run_model.jl:56")
    assert s["traceback"]["message"].startswith("ERROR: LoadError: Result index")
    assert s["n_errors"] == 1
    assert s["phase"] == "error"


def test_gurobi_mip_time_limit_learns_limit_and_flags_near_and_stall():
    w, events = feed(GUROBI_MIP_TIME_LIMIT, stall_seconds=50)
    s = w.snapshot()
    assert s["time_limit"] == 100.0
    near = of_kind(events, "time_limit_near")
    assert len(near) == 1 and near[0].data["elapsed_s"] >= 90
    assert s["time_limit_near"] is True
    stalls = of_kind(events, "stall")
    assert len(stalls) == 1
    assert stalls[0].data["reason"] == "no_improvement"
    assert stalls[0].data["idle_s"] >= 50
    assert stalls[0].data["gap"] == pytest.approx(0.054)
    assert s["stalled"] is True and s["stall_reason"] == "no_improvement"
    assert (s["status"], s["status_source"]) == ("TIME_LIMIT", "marker")
    assert of_kind(events, "solver_status")[-1].data["status"] == "TIME_LIMIT"
    assert s["nodes"] == 45
    assert s["best_obj"] == pytest.approx(91387430.12345)
    assert s["bound"] == pytest.approx(86449000.12345)
    assert s["last_gap"] == pytest.approx(0.054034)


def test_gurobi_lp_barrier_is_not_a_mip():
    w, events = feed(GUROBI_LP_BARRIER)
    s = w.snapshot()
    assert s["is_mip"] is False
    assert len(of_kind(events, "barrier_iter")) == 4
    assert of_kind(events, "barrier_iter")[-1].data["dual"] == pytest.approx(3.10203050e9)
    sol = of_kind(events, "solution")
    assert sol and sol[0].data["objective"] == pytest.approx(3.10203050e9)
    assert s["best_obj"] == pytest.approx(3.10203050e9)
    assert of_kind(events, "solver_status")[0].data["status"] == "OPTIMAL"
    assert (s["status"], s["status_source"]) == ("OPTIMAL", "marker")
    assert "postprocess" in [e.data["phase"] for e in of_kind(events, "phase")]
    assert s["solver_time_s"] == pytest.approx(30.12)


def test_gurobi_lp_infeasible_with_python_traceback_and_origin():
    w, events = feed(GUROBI_LP_INFEASIBLE_PYTHON)
    s = w.snapshot()
    assert of_kind(events, "solver_status")[0].data["status"] == "INFEASIBLE"
    tb = of_kind(events, "traceback")
    assert len(tb) == 1 and tb[0].data["language"] == "python"
    assert tb[0].data["message"] == "gurobipy.GurobiError: Unable to retrieve attribute 'objVal'"
    assert [f["func"] for f in tb[0].data["frames"]][:2] == ["<module>", "main"]
    assert tb[0].data["frames"][1]["line"] == 180
    marker = of_kind(events, "status")[-1]
    assert marker.data["status"] == "INFEASIBLE"
    assert marker.data["error_origin"] == "solver"
    assert s["error_origin"] == "solver"
    assert s["status"] == "INFEASIBLE"
    assert s["n_errors"] == 1
    assert s["errors"][0].startswith("gurobipy.GurobiError")


# ==========================================================================
# LogWatcher on the HiGHS dialect
# ==========================================================================

def test_highs_mip_optimal_synthetic():
    w, events = feed(HIGHS_MIP_OPTIMAL)
    s = w.snapshot()
    assert (s["solver"], s["solver_version"]) == ("highs", "1.7.2")
    assert s["is_mip"] is True
    assert s["model_size"]["rows"] == 641167
    assert s["model_size"]["presolved"]["binary"] == 201118
    assert s["model_size"]["presolved"]["continuous"] == 100098
    sizes = of_kind(events, "model_size")
    assert sizes[-1].data == {"rows": 612040, "cols": 301220, "binary": 201118, "integer": 4,
                              "implied_int": 0, "continuous": 100098, "nonzeros": 2101533}
    phases = [e.data["phase"] for e in of_kind(events, "phase")]
    assert phases.index("presolve") < phases.index("branch_and_bound") < phases.index("finished")
    inc = of_kind(events, "incumbent")
    assert [e.data["flag"] for e in inc] == ["R", "H", "L", "T", "L", "B"]
    assert s["n_incumbents"] == 6
    assert inc[0].data["incumbent"] == pytest.approx(3.289510e9)
    assert inc[0].data["gap"] == pytest.approx(37.2005)
    assert inc[-1].data["lp_iters"] == 420819
    assert inc[-1].data["explored_pct"] == 100.0
    prog = of_kind(events, "progress")
    assert len(prog) == 2
    assert prog[0].data["incumbent"] is None and prog[0].data["bound"] is None  # -inf / inf
    assert prog[1].data["bound"] == pytest.approx(8.721e7)
    # the source legend lines are not verdicts
    assert not of_kind(events, "error")
    # Solving report: verdict, bounds, gap, timing, nodes
    assert of_kind(events, "solver_status")[0].data["status"] == "OPTIMAL"
    assert of_kind(events, "solver_status")[0].data["solver_status_text"] == "Optimal"
    assert s["best_obj"] == pytest.approx(87786096.7294)
    assert s["bound"] == pytest.approx(87311726.6708)
    assert s["last_gap"] == pytest.approx(0.0054)
    assert s["solver_time_s"] == pytest.approx(170.40)
    assert s["nodes"] == 1
    sol = [e for e in of_kind(events, "solution") if e.data.get("final")]
    assert sol[-1].data["objective"] == pytest.approx(87786096.7294)
    assert (s["status"], s["status_source"]) == ("OPTIMAL", "marker")


def test_highs_mip_time_limit_stall_and_near():
    w, events = feed(HIGHS_MIP_TIME_LIMIT, stall_seconds=500, time_limit=900)
    s = w.snapshot()
    stalls = of_kind(events, "stall")
    assert len(stalls) == 1
    assert stalls[0].data["reason"] == "no_improvement"
    assert 500 <= stalls[0].data["idle_s"] < 600
    assert stalls[0].data["incumbent"] == pytest.approx(9.138743e7)
    near = of_kind(events, "time_limit_near")
    assert len(near) == 1 and near[0].data["time_limit"] == 900.0
    assert s["stalled"] is True and s["time_limit_near"] is True
    assert of_kind(events, "solver_status")[0].data["status"] == "TIME_LIMIT"
    assert (s["status"], s["status_source"]) == ("TIME_LIMIT", "marker")
    assert s["best_obj"] == pytest.approx(91387430.12)
    assert s["bound"] == pytest.approx(86449000.0)
    assert s["last_gap"] == pytest.approx(0.0571)
    assert s["nodes"] == 88


def test_highs_mip_infeasible_in_presolve():
    w, events = feed(HIGHS_MIP_INFEASIBLE)
    s = w.snapshot()
    verdicts = of_kind(events, "solver_status")
    assert [e.data["status"] for e in verdicts] == ["INFEASIBLE", "INFEASIBLE"]
    assert verdicts[0].message == "Presolve: Infeasible"
    assert s["best_obj"] is None and s["bound"] is None
    assert not of_kind(events, "solution")          # "inf" bounds are not a solution
    assert s["nodes"] == 0
    assert (s["status"], s["status_source"]) == ("INFEASIBLE", "marker")


def test_highs_lp_optimal():
    w, events = feed(HIGHS_LP_OPTIMAL)
    s = w.snapshot()
    assert s["is_mip"] is False
    assert s["model_size"] == {"rows": 801100, "cols": 320020, "nonzeros": 2674308, "kind": "LP",
                               "presolved": {"rows": 612040, "cols": 301220, "nonzeros": 2101533}}
    rows = of_kind(events, "simplex_iter")
    assert [e.data["iter"] for e in rows] == [0, 20000, 48090]
    assert rows[0].data["primal_infeas"] == 1388 and rows[0].data["dual_infeas"] == 0
    assert rows[-1].data["primal_infeas"] == 0 and rows[-1].data["time_s"] == pytest.approx(15.2)
    phases = [e.data["phase"] for e in of_kind(events, "phase")]
    assert phases == ["presolve", "simplex", "finished"]
    assert of_kind(events, "solver_status")[0].data["status"] == "OPTIMAL"
    assert s["best_obj"] == pytest.approx(418.75012738)
    assert s["lp_objective"] == pytest.approx(418.75012738)
    assert s["solver_time_s"] == pytest.approx(15.44)
    assert (s["status"], s["status_source"]) == ("OPTIMAL", "marker")


def test_highs_114_lp_infeasible_verbatim():
    w, events = feed(HIGHS_LP_INFEASIBLE_114)
    s = w.snapshot()
    assert (s["solver"], s["solver_version"]) == ("highs", "1.14.0")
    assert s["is_mip"] is False
    assert s["n_warnings"] == 2 and s["warnings"][0].startswith("WARNING: Problem has")
    verdicts = of_kind(events, "solver_status")
    assert verdicts[0].message.startswith("Problem status detected on presolve")
    assert [e.data["status"] for e in verdicts] == ["INFEASIBLE"] * 3
    assert s["best_obj"] is None                      # the 0.0 objective is ignored
    rows = of_kind(events, "simplex_iter")
    assert [e.data["iter"] for e in rows] == [0, 48090]  # the Pr-less row parses too
    assert "primal_infeas" not in rows[1].data
    assert "simplex" in [e.data["phase"] for e in of_kind(events, "phase")]
    assert s["solver_time_s"] == pytest.approx(3.91)
    assert (s["status"], s["status_source"]) == ("INFEASIBLE", "marker")
    assert s["error_origin"] is None


def test_linopy_wrapper_lines_do_not_override_the_verdict():
    w, events = feed(LINOPY_WRAPPER)
    s = w.snapshot()
    statuses = [(e.kind, e.data["status"]) for e in events if e.kind in ("status", "solver_status")]
    assert statuses == [("solver_status", "OPTIMAL"), ("status", "OPTIMAL"), ("status", "OPTIMAL")]
    assert s["best_obj"] == pytest.approx(1.23456789e6)
    assert "write_outputs" in [e.data["phase"] for e in of_kind(events, "phase")]
    assert s["status"] == "OPTIMAL"


# ==========================================================================
# generic lines: warnings, errors, preflight, stage markers
# ==========================================================================

@pytest.mark.parametrize("line", [
    "WARNING: export_price (100.0) > import_price (59.0): exports worth more than imports",
    "Warning: Concurrent optimizer requires crossover - forcing it on",
    "┌ Warning: a Julia-style warning",
    "[toy] WARNING: something odd in the inputs",
])
def test_warning_lines(line):
    w = LogWatcher(stall_seconds=None)
    events = w.feed(line)
    assert kinds(events) == ["warning"]
    assert w.n_warnings == 1 and w.warnings[0] == line.strip()


@pytest.mark.parametrize("line", [
    "[stderr]",
    "Academic license - for non-commercial use only - expires 2027-03-13",
    "Copyright (c) 2024 HiGHS under MIT licence terms",
    "  Activating project at `~/models/x`",
    "Solution count 0",
])
def test_benign_lines_produce_nothing(line):
    w = LogWatcher(stall_seconds=None)
    assert w.feed(line) == []
    assert w.status is None and w.n_errors == 0 and w.n_warnings == 0


def test_license_failure_is_an_error():
    w = LogWatcher(stall_seconds=None)
    events = w.feed("No Gurobi license found (user x, host y, hostid z, cores 8)")
    assert kinds(events) == ["error"] and events[0].data["reason"] == "license"
    assert w.n_errors == 1


def test_bare_julia_error_without_stacktrace():
    w = LogWatcher(stall_seconds=None)
    assert kinds(w.feed("ERROR: boom")) == ["error"]
    assert w.feed("Model status        : Optimal")[-1].kind == "solver_status"
    assert w.finish() == []
    assert w.traceback is None and w.n_errors == 1


def test_unterminated_python_traceback_is_flushed_by_finish():
    w = LogWatcher(stall_seconds=None)
    assert w.feed("Traceback (most recent call last):") == []
    assert w.feed('  File "/w/d.py", line 3, in <module>') == []
    events = w.finish()
    assert kinds(events) == ["traceback"]
    assert events[0].data["message"] == "traceback (truncated)"
    assert w.finished is True


def test_preflight_markers():
    w = LogWatcher(stall_seconds=None)
    events = w.feed("Preflight checks passed for demo_case")
    assert kinds(events) == ["phase", "preflight"]
    assert events[1].data["passed"] is True
    assert w.phase == "preflight" and w.status is None

    w = LogWatcher(stall_seconds=None)
    events = w.feed("PREFLIGHT FAILED")
    assert of_kind(events, "preflight")[0].data["passed"] is False
    assert w.status == "ERROR" and w.error_origin == "preflight"
    assert of_kind(events, "status")[0].data["error_origin"] == "preflight"
    assert w.feed("missing required keys: island") == []  # after the verdict: quiet


def test_stage_marker_sets_phase():
    w = LogWatcher(stall_seconds=None)
    events = w.feed("[toy] stage: build_network")
    assert kinds(events) == ["phase"]
    assert events[0].data == {"phase": "build_network", "previous": "starting", "source": "marker"}
    assert w.feed("[toy] stage: build_network") == []   # unchanged phase: no event


# ==========================================================================
# stall / time-limit-near on wall time (tick with an injected clock)
# ==========================================================================

def test_output_stall_via_tick_and_recovery():
    clock = [0.0]
    w = LogWatcher(stall_seconds=10, clock=lambda: clock[0])
    w.feed("Running HiGHS 1.7.2 (git hash: abc): Copyright (c) 2024 HiGHS under MIT licence terms")
    clock[0] = 5.0
    assert w.tick() == []
    clock[0] = 11.0
    events = w.tick()
    assert kinds(events) == ["stall"]
    assert events[0].data["reason"] == "no_output"
    assert events[0].data["idle_s"] == pytest.approx(11.0)
    assert events[0].wall_s == pytest.approx(11.0)
    assert w.stalled is True
    assert w.tick() == []                       # flagged once
    w.feed("Presolving model")                  # output resumes
    assert w.stalled is False
    assert w.n_stalls == 1


def test_no_stall_after_finish():
    clock = [0.0]
    w = LogWatcher(stall_seconds=10, clock=lambda: clock[0])
    w.feed("Model status        : Optimal")
    clock[0] = 100.0
    assert w.tick() == []


def test_time_limit_near_from_wall_clock():
    clock = [0.0]
    w = LogWatcher(stall_seconds=None, time_limit=100, clock=lambda: clock[0])
    clock[0] = 50.0
    assert w.tick() == []
    clock[0] = 91.0
    events = w.tick()
    assert kinds(events) == ["time_limit_near"]
    assert events[0].data["fraction"] == pytest.approx(0.9)
    assert events[0].data["elapsed_s"] == pytest.approx(91.0)
    clock[0] = 99.0
    assert w.tick() == []                       # fires once


def test_improvement_stall_uses_solver_time_and_recovers():
    rows = [
        "H    0     0                    9.138743e+07 8.6107e+07  5.78%     -   21s",
        "     0     2 8.6449e+07    0 3973 9.1387e+07 8.6449e+07  5.40%     -   40s",
        "    10     8 8.6449e+07    3 3811 9.1387e+07 8.6449e+07  5.40%  1204   70s",
        "    31    20 8.6449e+07    5 3702 9.1387e+07 8.6449e+07  5.40%   988   92s",
        "H   40    25                    9.000000e+07 8.6449e+07  4.10%   900  100s",
        "    60    30 8.6449e+07    6 3600 9.0000e+07 8.6449e+07  4.10%   900  110s",
    ]
    w = LogWatcher(stall_seconds=50)
    events = w.feed_many(rows)
    stalls = of_kind(events, "stall")
    assert len(stalls) == 1 and stalls[0].data["idle_s"] == pytest.approx(52.0)
    assert stalls[0].message.startswith("no incumbent/bound improvement")
    assert w.stalled is False                   # the H row cleared it
    assert w.n_stalls == 1 and w.n_incumbents == 2


# ==========================================================================
# RunEvent
# ==========================================================================

def test_run_event_str_and_dict():
    ev = RunEvent(kind="incumbent", wall_s=12.5,
                  message="H 0 0 ... 20s", data={"incumbent": 2.179905e8, "gap_pct": 60.5,
                                                 "nodes": 0, "time_s": 20.0})
    text = str(ev)
    assert "incumbent" in text and "gap_pct=60.5" in text and "12.5s" in text
    assert ev.to_dict() == {"kind": "incumbent", "wall_s": 12.5, "message": "H 0 0 ... 20s",
                            "data": {"incumbent": 2.179905e8, "gap_pct": 60.5, "nodes": 0,
                                     "time_s": 20.0}}
    assert "OPTIMAL" in str(RunEvent("status", 1.0, "x solved successfully",
                                     {"status": "OPTIMAL"}))


# ==========================================================================
# RunMonitor
# ==========================================================================

def test_run_monitor_fanout_summary_and_persistence(tmp_path):
    seen = []
    monitor = RunMonitor(tmp_path, on_event=seen.append, stall_seconds=None, keep_events=10)
    events = monitor.feed_many(HIGHS_MIP_OPTIMAL.splitlines(keepends=True))
    summary = monitor.finish(StreamResult(returncode=0, wall_seconds=171.2))
    assert len(seen) == monitor.n_events == len(events) + 0
    assert summary["event_counts"]["incumbent"] == 6
    assert summary["n_events"] == sum(summary["event_counts"].values())
    assert summary["status"] == "OPTIMAL" and summary["source"] == "live"
    assert summary["returncode"] == 0 and summary["wall_seconds"] == 171.2
    assert summary["aborted"] is False and summary["timed_out"] is False
    assert summary["callback_errors"] == 0
    assert "updated_at" in summary and summary["schema_version"] == 1
    assert len(monitor.events) == 10            # bounded ring (17 events total)
    saved = RunMonitor.load(monitor.path)
    assert saved["status"] == "OPTIMAL"
    assert saved["best_obj"] == pytest.approx(87786096.7294)
    assert len(saved["recent_events"]) == 10
    assert saved["recent_events"][-1]["kind"] in NOTABLE_KINDS | {"progress"}
    assert not (tmp_path / "monitor.json.tmp").exists()


def test_run_monitor_persists_on_notable_events_only(tmp_path):
    clock = [0.0]
    monitor = RunMonitor(tmp_path, stall_seconds=None, persist_interval=1000.0,
                         clock=lambda: clock[0])
    assert monitor.path == tmp_path / "monitor.json"
    monitor.on_line("     0     0 8.6107e+07    0 5081 3.2895e+09 8.6107e+07  97.4%     -   19s")
    # a first event always persists once (no previous snapshot) ...
    first = RunMonitor.load(monitor.path)
    assert first["n_events"] >= 1
    # ... plain progress rows afterwards do not (interval not due)
    monitor.on_line("     0     0 8.6449e+07    0 3973 3.2895e+09 8.6449e+07  97.3%     -   26s")
    assert RunMonitor.load(monitor.path)["n_events"] == first["n_events"]
    # a notable event (new incumbent) persists immediately
    monitor.on_line("H    0     0                    9.138743e+07 8.6449e+07  5.40%     -   30s")
    assert RunMonitor.load(monitor.path)["n_incumbents"] == 1


def test_run_monitor_listener_errors_are_counted_not_raised(tmp_path):
    def bad(_ev):
        raise RuntimeError("listener bug")

    monitor = RunMonitor(None, on_event=bad, stall_seconds=None)
    events = monitor.on_line("Running HiGHS 1.7.2 (git hash: abc): Copyright (c) 2024")
    assert kinds(events) == ["solver_start"]
    assert monitor.callback_errors == 1
    assert monitor.path is None
    with pytest.raises(ValueError):
        monitor.save()


def test_run_monitor_abort_and_stream_result_fold_in():
    monitor = RunMonitor(stall_seconds=None)
    assert not monitor.abort_event.is_set()
    monitor.request_abort("gap flat for 20 min")
    assert monitor.abort_event.is_set()
    summary = monitor.finish(StreamResult(returncode=-15, wall_seconds=3.0, aborted=True,
                                          abort_reason="abort event"))
    assert summary["aborted"] is True and summary["abort_reason"] == "gap flat for 20 min"

    monitor = RunMonitor(stall_seconds=None)
    summary = monitor.finish(StreamResult(returncode=None, wall_seconds=9.0, timed_out=True,
                                          aborted=True, abort_reason="abort file"))
    assert summary["timed_out"] is True and summary["abort_reason"] == "abort file"


def test_run_monitor_rejects_watcher_and_kwargs_together():
    with pytest.raises(ValueError):
        RunMonitor(watcher=LogWatcher(), stall_seconds=5)


def test_replay_log_from_path_and_lines(tmp_path):
    path = write_log(tmp_path, GUROBI_MILP)
    seen = []
    monitor = replay_log(path, on_event=seen.append, run_dir=tmp_path, stall_seconds=None)
    summary = monitor.summary()
    assert summary["source"] == "replay" and summary["status"] == "OPTIMAL"
    assert len(seen) == summary["n_events"] > 0
    saved = json.loads((tmp_path / "monitor.json").read_text())
    assert saved["source"] == "replay" and saved["n_incumbents"] == 8
    # iterable-of-lines form, no persistence
    monitor2 = replay_log(HIGHS_LP_OPTIMAL.splitlines(), stall_seconds=None)
    assert monitor2.summary()["status"] == "OPTIMAL" and monitor2.path is None


@pytest.mark.skipif(not REAL_GUROBI_MILP.is_file(), reason="local run log not present")
def test_real_gurobi_milp_log():
    s = replay_log(REAL_GUROBI_MILP, stall_seconds=None).summary()
    assert (s["solver"], s["solver_version"], s["is_mip"]) == ("gurobi", "13.0.0", True)
    assert s["status"] == "OPTIMAL"
    assert s["n_incumbents"] == 12
    assert s["best_obj"] == pytest.approx(87786096.72942)
    assert s["bound"] == pytest.approx(87311726.67084)
    assert s["last_gap"] == pytest.approx(0.005404)
    assert s["nodes"] == 1 and s["time_limit"] == 259200.0
    assert s["solver_time_s"] == pytest.approx(170.4)
    assert s["event_counts"]["barrier_iter"] == 76
    assert s["model_size"]["rows"] == 799700
    assert s["n_warnings"] == 1 and s["n_errors"] == 0 and s["traceback"] is None


@pytest.mark.skipif(not REAL_GUROBI_INFEASIBLE.is_file(), reason="local run log not present")
def test_real_gurobi_infeasible_log():
    s = replay_log(REAL_GUROBI_INFEASIBLE, stall_seconds=None).summary()
    assert s["status"] == "INFEASIBLE" and s["best_obj"] is None and s["nodes"] == 0
    assert s["traceback"]["language"] == "julia"
    assert len(s["traceback"]["frames"]) == 15
    assert s["traceback"]["at"].endswith("run_model.jl:56")
    assert s["n_errors"] == 1 and s["phase"] == "error"


@pytest.mark.skipif(not REAL_HIGHS_LP_INFEASIBLE.is_file(), reason="local run log not present")
def test_real_highs_lp_infeasible_log():
    monitor = replay_log(REAL_HIGHS_LP_INFEASIBLE, stall_seconds=None)
    s = monitor.summary()
    assert (s["solver"], s["solver_version"], s["is_mip"]) == ("highs", "1.14.0", False)
    assert s["status"] == "INFEASIBLE" and s["best_obj"] is None
    assert s["n_warnings"] == 2
    assert s["model_size"]["rows"] == 801100 and s["model_size"]["kind"] == "LP"
    assert s["event_counts"]["simplex_iter"] == 2
    assert s["solver_time_s"] == pytest.approx(3.91)
    first_verdict = [e for e in monitor.events if e.kind == "solver_status"][0]
    assert first_verdict.message.startswith("Problem status detected on presolve")


# ==========================================================================
# stream_command
# ==========================================================================

CHILD_LINES = """\
import sys, time
for i in range(5):
    print(f"line {i}", flush=True)
    time.sleep(0.15)
print("done", file=sys.stderr, flush=True)
"""

CHILD_SLEEP = "import time; print('start', flush=True); time.sleep(20)"


def test_stream_command_tees_lines_as_they_arrive(tmp_path):
    log = tmp_path / "solver.log"
    arrivals, seen_pid = [], []

    def on_line(line):
        arrivals.append((time.monotonic(), line))
        seen_pid.append((tmp_path / "run.pid").exists())

    result = stream_command(python_child(CHILD_LINES), cwd=tmp_path, log_path=log,
                            on_line=on_line)
    assert result.ok and result.returncode == 0 and not result.timed_out and not result.aborted
    assert result.n_lines == 6 and result.callback_errors == 0
    assert result.log_path == str(log)
    lines = [line for _, line in arrivals]
    assert lines == [f"line {i}\n" for i in range(5)] + ["done\n"]  # stderr merged, newline kept
    assert log.read_text() == "".join(lines)
    # streamed live, not dumped at exit: the first and fifth line are >= 0.3 s apart
    assert arrivals[4][0] - arrivals[0][0] >= 0.3
    assert result.wall_seconds >= 0.6
    assert all(seen_pid)                          # run.pid present while the child runs
    assert not (tmp_path / "run.pid").exists()    # and gone afterwards
    assert not (tmp_path / "ABORT").exists()


def test_stream_command_without_log_path_and_nonzero_exit():
    got = []
    result = stream_command(python_child("import sys; print('x'); sys.exit(3)"), on_line=got.append)
    assert result.returncode == 3 and not result.ok
    assert got == ["x\n"] and result.log_path is None


def test_stream_command_timeout(tmp_path):
    result = stream_command(python_child(CHILD_SLEEP), log_path=tmp_path / "solver.log",
                            timeout=0.5, grace_seconds=1.0)
    assert result.timed_out and not result.aborted
    assert result.returncode != 0
    assert result.wall_seconds < 10
    assert (tmp_path / "solver.log").read_text() == "start\n"
    assert not (tmp_path / "run.pid").exists()


def test_stream_command_abort_event(tmp_path):
    abort = threading.Event()
    threading.Timer(0.3, abort.set).start()
    result = stream_command(python_child(CHILD_SLEEP), log_path=tmp_path / "solver.log",
                            abort=abort, grace_seconds=1.0)
    assert result.aborted and result.abort_reason == "abort event"
    assert not result.timed_out and result.returncode != 0
    assert result.wall_seconds < 10


def test_stream_command_abort_file(tmp_path):
    abort_path = tmp_path / "ABORT"
    abort_path.write_text("stale\n")             # removed at start, must not abort immediately
    abort = threading.Event()

    def write_abort():
        abort_path.write_text("gap flat for 20 min\n2026-09-04T12:00:00\n")

    threading.Timer(0.4, write_abort).start()
    result = stream_command(python_child(CHILD_SLEEP), log_path=tmp_path / "solver.log",
                            abort=abort, grace_seconds=1.0)
    assert result.aborted and result.abort_reason == "gap flat for 20 min"
    assert abort.is_set()                         # the caller's event is set too
    assert result.wall_seconds >= 0.3


def test_stream_command_ticks_and_swallows_callback_errors(tmp_path):
    ticks = []

    def on_line(_line):
        raise ValueError("monitor bug")

    result = stream_command(python_child(CHILD_LINES), log_path=tmp_path / "solver.log",
                            on_line=on_line, on_tick=lambda: ticks.append(time.monotonic()),
                            tick_interval=0.1, pid_file=False, abort_file=False)
    assert result.returncode == 0
    assert result.callback_errors == 6            # every line's callback raised
    assert result.n_lines == 6
    assert (tmp_path / "solver.log").read_text().startswith("line 0\n")  # tee unaffected
    assert len(ticks) >= 2


def test_request_abort_stops_a_running_child(tmp_path):
    results = {}

    def target():
        results["r"] = stream_command(python_child(CHILD_SLEEP), log_path=tmp_path / "solver.log",
                                      grace_seconds=1.0)

    worker = threading.Thread(target=target)
    worker.start()
    deadline = time.monotonic() + 10
    while not (tmp_path / "run.pid").exists() and time.monotonic() < deadline:
        time.sleep(0.02)
    assert (tmp_path / "run.pid").exists()
    info = request_abort(tmp_path, reason="test abort", grace_seconds=3.0)
    worker.join(timeout=20)
    assert not worker.is_alive()
    assert info["pid"] is not None and info["alive_before"] is True
    assert info["alive_after"] is False
    assert (tmp_path / "ABORT").read_text().startswith("test abort\n")
    r = results["r"]
    assert r.aborted and r.abort_reason == "test abort"


def test_request_abort_without_a_run(tmp_path):
    info = request_abort(tmp_path / "fresh", reason="nothing running")
    assert info["pid"] is None and info["alive_before"] is False and info["signalled"] is False
    assert (tmp_path / "fresh" / "ABORT").is_file()


# ==========================================================================
# runner wiring: Execution.monitor, on_event, replay for legacy adapters
# ==========================================================================

class _Base(Adapter):
    def validate_config(self, config):
        if config.get("bad"):
            return ValidationResult(ok=False, errors=["bad key"])
        return ValidationResult(ok=True)

    def intervention_spec(self):
        return InterventionSpec(tier_a_keys={"mipgap", "time_limit"}, tier_c_keys={"cap"})

    def locate_outputs(self, run_dir):
        return {}


class StreamingAdapter(_Base):
    """Monitors live: builds a RunMonitor around ``on_event`` (the wiring a
    streaming adapter uses) and returns ``Execution(monitor=summary)``."""

    name = "streaming"
    log = HIGHS_MIP_OPTIMAL

    def __init__(self):
        self.got_listener = None

    def run(self, config, run_dir, on_event=None):
        self.got_listener = on_event
        run_dir = Path(run_dir)
        monitor = RunMonitor(run_dir, on_event=on_event, stall_seconds=None,
                             time_limit=config.get("time_limit"))
        lines = self.log.splitlines(keepends=True)
        write_log(run_dir, self.log)
        for line in lines:
            monitor.on_line(line)
        summary = monitor.finish(StreamResult(returncode=0, wall_seconds=1.0))
        return Execution(termination_status="OPTIMAL", wall_seconds=1.0, solver_log="solver.log",
                         returncode=0, monitor=summary)


class LegacyAdapter(_Base):
    """The two-argument signature: no live events, the runner replays the log."""

    name = "legacy"

    def run(self, config, run_dir):
        write_log(Path(run_dir), GUROBI_MILP)
        return Execution(termination_status="OPTIMAL", wall_seconds=2.5, solver_log="solver.log",
                         returncode=0)


class KwargsAdapter(_Base):
    name = "kwargs"

    def run(self, config, run_dir, **kwargs):
        write_log(Path(run_dir), HIGHS_LP_OPTIMAL)
        return Execution(termination_status="OPTIMAL", solver_log="solver.log", returncode=0)


def test_adapter_accepts_on_event_detection():
    assert adapter_accepts_on_event(StreamingAdapter()) is True
    assert adapter_accepts_on_event(KwargsAdapter()) is True
    assert adapter_accepts_on_event(LegacyAdapter()) is False


def test_run_and_record_streaming_adapter_delivers_live_once(tmp_path):
    seen = []
    adapter = StreamingAdapter()
    record = run_and_record(adapter, {"time_limit": 300}, tmp_path / "r1", on_event=seen.append)
    mon = record.execution.monitor
    assert mon["source"] == "live" and mon["status"] == "OPTIMAL"
    assert mon["time_limit"] == 300.0
    assert len(seen) == mon["n_events"] > 0        # exactly once, no replay on top
    assert record.execution.mipgap_reached == pytest.approx(0.0054)
    assert (tmp_path / "r1" / "monitor.json").is_file()
    loaded = RunRecord.load(tmp_path / "r1" / "run_record.json")
    assert loaded.execution.monitor["best_obj"] == pytest.approx(87786096.7294)
    assert loaded.execution.termination_status == "OPTIMAL"


def test_run_and_record_gives_streaming_adapters_a_listener_by_default(tmp_path):
    adapter = StreamingAdapter()
    record = run_and_record(adapter, {}, tmp_path / "r2")
    assert adapter.got_listener is not None      # so the adapter monitors live regardless
    assert record.execution.monitor["source"] == "live"
    assert json.loads((tmp_path / "r2" / "monitor.json").read_text())["status"] == "OPTIMAL"


def test_run_and_record_legacy_adapter_replays_log(tmp_path):
    seen = []
    record = run_and_record(LegacyAdapter(), {}, tmp_path / "r3", on_event=seen.append)
    mon = record.execution.monitor
    assert mon["source"] == "replay" and mon["status"] == "OPTIMAL"
    assert len(seen) == mon["n_events"] > 0        # the listener still sees every event
    assert mon["returncode"] == 0 and mon["wall_seconds"] == 2.5
    assert record.execution.mipgap_reached == pytest.approx(0.005404)
    saved = json.loads((tmp_path / "r3" / "monitor.json").read_text())
    assert saved["returncode"] == 0 and saved["wall_seconds"] == 2.5 and saved["source"] == "replay"
    # the persisted record round-trips through from_dict
    loaded = RunRecord.load(tmp_path / "r3" / "run_record.json")
    assert loaded.execution.monitor["n_incumbents"] == 8


def test_run_and_record_kwargs_adapter_without_monitor_field_gets_replay(tmp_path):
    seen = []
    record = run_and_record(KwargsAdapter(), {}, tmp_path / "r4", on_event=seen.append)
    assert record.execution.monitor["source"] == "replay"
    assert record.execution.monitor["status"] == "OPTIMAL"
    assert seen and seen[0].kind == "solver_start"


def test_run_and_record_preflight_failure_has_no_monitor(tmp_path):
    record = run_and_record(LegacyAdapter(), {"bad": True}, tmp_path / "r5")
    assert record.execution.termination_status == "ERROR"
    assert record.execution.error_origin == "preflight"
    assert record.execution.monitor is None
    # but the preflight log is still watchable
    assert replay_log(tmp_path / "r5" / "solver.log").summary()["error_origin"] == "preflight"


def test_attach_monitor_summary_is_advisory(tmp_path):
    ex = Execution(termination_status="OPTIMAL", solver_log="missing.log")
    assert attach_monitor_summary(ex, tmp_path).monitor is None      # no log: untouched
    ex = Execution(termination_status="OPTIMAL", monitor={"last_gap": 0.02})
    assert attach_monitor_summary(ex, tmp_path).mipgap_reached == pytest.approx(0.02)


def test_execution_monitor_is_backward_compatible():
    old = {"config": {"a": 1}, "execution": {"termination_status": "OPTIMAL", "returncode": 0}}
    record = RunRecord.from_dict(old)
    assert record.execution.monitor is None
    record.execution.monitor = {"status": "OPTIMAL", "last_gap": 0.01}
    again = RunRecord.from_dict(json.loads(json.dumps(record.to_dict())))
    assert again.execution.monitor == {"status": "OPTIMAL", "last_gap": 0.01}


# ==========================================================================
# python -m framework.watch
# ==========================================================================

def _finished_run(tmp_path: Path, log: str = HIGHS_MIP_OPTIMAL) -> Path:
    run_dir = tmp_path / "run"
    write_log(run_dir, log)
    RunRecord(config={"time_limit": 100}, execution=Execution(
        termination_status="OPTIMAL", wall_seconds=171.0, solver_log="solver.log", returncode=0,
    )).save(run_dir / "run_record.json")
    return run_dir


def test_watch_summarises_a_finished_run(tmp_path):
    run_dir = _finished_run(tmp_path)
    out = io.StringIO()
    summary = watch_mod.watch(run_dir, out=out)
    assert summary["status"] == "OPTIMAL" and summary["run_record_status"] == "OPTIMAL"
    assert summary["log_lines"] > 20 and summary["pid"] is None
    assert summary["source"] == "watch"
    text = out.getvalue()
    assert "status:   OPTIMAL" in text
    assert "finished (returncode 0" in text
    assert "events (last" in text and "incumbent" in text


def test_watch_time_limit_hint_from_config(tmp_path):
    run_dir = tmp_path / "run"
    write_log(run_dir, GUROBI_MIP_TIME_LIMIT.replace("Set parameter TimeLimit to value 100\n", ""))
    (run_dir / "config.json").write_text(json.dumps({"time_limit": 100}))
    summary = watch_mod.watch(run_dir, quiet=True)
    assert summary["time_limit"] == 100.0 and summary["time_limit_near"] is True
    assert summary["status"] == "TIME_LIMIT"


def test_watch_main_json_abort_and_missing(tmp_path, capsys):
    run_dir = _finished_run(tmp_path)
    assert watch_mod.main([str(run_dir), "--json", "--events", "3"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "OPTIMAL" and payload["run_dir"] == str(run_dir.resolve())
    assert len(payload["recent_events"]) == 3
    assert all(e["kind"] in NOTABLE_KINDS for e in payload["recent_events"])

    assert watch_mod.main([str(run_dir), "--abort", "gap flat"]) == 0
    assert (run_dir / "ABORT").read_text().startswith("gap flat\n")
    assert "abort file written" in capsys.readouterr().out

    assert watch_mod.main([str(tmp_path / "nope")]) == 2


def test_watch_follow_ends_when_the_run_finishes(tmp_path):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    log = run_dir / "solver.log"
    log.write_text("")
    chunks = HIGHS_MIP_OPTIMAL.splitlines(keepends=True)
    step = max(1, len(chunks) // 4)

    def writer():
        for i in range(0, len(chunks), step):
            with log.open("a") as fh:
                fh.write("".join(chunks[i:i + step]))
            time.sleep(0.1)
        RunRecord(config={}, execution=Execution(termination_status="OPTIMAL", returncode=0)) \
            .save(run_dir / "run_record.json")

    threading.Thread(target=writer).start()
    out = io.StringIO()
    t0 = time.monotonic()
    summary = watch_mod.watch(run_dir, follow=True, interval=0.05, max_seconds=15, out=out)
    assert time.monotonic() - t0 < 15
    assert summary["status"] == "OPTIMAL" and summary["n_incumbents"] == 6
    text = out.getvalue()
    assert text.startswith("following ")
    assert "incumbent" in text and "solver_status" in text


def test_watch_tail_handles_partial_lines_and_truncation(tmp_path):
    path = tmp_path / "solver.log"
    path.write_text("a\nb")
    tail = watch_mod._Tail(path)
    assert tail.read_new_lines() == ["a"]
    with path.open("a") as fh:
        fh.write("c\nd\n")
    assert tail.read_new_lines() == ["bc", "d"]
    path.write_text("z\n")                        # rewritten shorter: start over
    assert tail.read_new_lines() == ["z"]
    assert tail.read_new_lines() == []
