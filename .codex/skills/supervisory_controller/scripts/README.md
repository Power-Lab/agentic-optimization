# Supervisory Controller — Scripts

The Supervisory Controller is an orchestration skill: it makes decisions and routes work to other skills. It does not perform heavy computation itself, so no standalone Python helper scripts are provided.

## Extension Points

If you want to implement the controller as a Python process, wire it as follows:

```python
# controller_loop.py — skeleton for embedding the supervisory controller

from scripts.run_agent_workflow import run_session

# The full agent loop in scripts/run_agent_workflow.py already implements
# the supervisory controller logic end-to-end. Key entry points:

# 1. Pre-solver budget check
def check_budget(iteration_state: dict) -> str:
    """Returns action: LAUNCH_SOLVER | TERMINATE_FAILURE"""
    if iteration_state["current_iteration"] >= iteration_state["max_iterations"]:
        return "TERMINATE_FAILURE"
    if iteration_state["elapsed_wall_time_s"] >= iteration_state["wall_time_budget_s"] * 0.85:
        return "TERMINATE_FAILURE"
    return "LAUNCH_SOLVER"

# 2. Plateau check
def check_plateau(convergence_trend: list) -> bool:
    """Returns True if last 3 improvements are all < 0.1%."""
    if len(convergence_trend) < 3:
        return False
    vals = [e["objective_value_USD"] for e in convergence_trend[-3:]]
    improvements = [abs(vals[i] - vals[i+1]) / max(vals[i], 1) for i in range(2)]
    return all(imp < 0.001 for imp in improvements)

# 3. Stall detection (called every 30 s during solver polling)
def check_stall(gap_history: list[float]) -> bool:
    """Returns True if last 5 gap readings improve by < 0.01%."""
    if len(gap_history) < 5:
        return False
    improvements = [abs(gap_history[i] - gap_history[i+1]) for i in range(-5, -1)]
    return all(imp < 0.0001 for imp in improvements)
```

## Relevant Scripts in the Root `scripts/` Directory

- `scripts/run_agent_workflow.py` — full session orchestration loop (implements the controller)
- `scripts/stream_log_monitor.py` — live solver log tailing (monitoring feed)
- `scripts/terminate_run.py` — graceful solver termination signal
- `scripts/run_history_tracker.py` — iteration memory read/write
