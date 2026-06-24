# Convergence Theory Reference

Background on the convergence concepts used by the Iteration Memory skill and the Supervisory Controller's termination logic.

---

## 1. What Is Convergence in This Context?

In the LLM-assisted energy optimization workflow, "convergence" has two distinct meanings:

**A. Solver convergence (within a single run):**
The MIP solver's lower bound (best bound) approaches the upper bound (best incumbent) until they are within the MIP gap tolerance. This is handled by Gurobi and reported in the solver log.

**B. Iteration convergence (across multiple runs):**
The sequence of best feasible objective values across successive refinement iterations is improving at a decreasing rate, eventually plateauing. This is what the Iteration Memory skill tracks.

This reference covers **B — iteration convergence**.

---

## 2. Plateau Detection

**Definition:** A plateau is detected when the objective value improvement across the last 3 consecutive feasible iterations is all less than 0.1%.

**Formula:**
```
improvement[i] = |objective[i-1] - objective[i]| / objective[i-1]

plateau = (improvement[-3] < 0.001) AND
          (improvement[-2] < 0.001) AND
          (improvement[-1] < 0.001)
```

**Why 3 iterations?**
- 1 iteration: too noisy (single lucky or unlucky run)
- 2 iterations: still noisy (one-time event)
- 3 iterations: sufficient statistical confidence that improvement has genuinely slowed
- 5+ iterations: too conservative, wastes computational budget

**Why 0.1%?**
- For a $163,249 baseline, 0.1% = $163 — well below the practical significance threshold for power system planning
- Typical solver MIP gap is 0.1%, so further improvement from parameter tuning is unlikely to exceed 0.1% of objective
- Standard practice in iterative power system optimization heuristics

---

## 3. Improvement Percentage Calculation

**Last iteration improvement:**
```
improvement_pct_last = (objective[n-2] - objective[n-1]) / objective[n-2] × 100
```

**Total improvement from first to last feasible run:**
```
improvement_pct_total = (objective_first - objective_last) / objective_first × 100
```

**Example from session_20240102_001:**
```
Run 103: $148,200 (first feasible)
Run 104: $148,010

improvement_pct_last = (148200 - 148010) / 148200 × 100 = 0.128%

plateau_check: 0.128% < 0.1%? NO — not yet at plateau with only 2 feasible runs
```
With a 3rd feasible run at $148,000, the improvement would be 0.007%, triggering plateau detection.

---

## 4. Pattern Deduplication

**Key design decision:** The failure pattern hash is computed from `scenario_config_hash + cause`, NOT just from `cause` alone.

**Why?** Two runs may fail for the "same" cause (`EMISSION_CAP_TOO_TIGHT`) but with different emission caps (800 vs. 1000 tCO2). These are different patterns — the fix for one (relax to 1000) is already applied in the second. Using the composite key correctly identifies these as distinct patterns and applies escalated fixes.

**SHA-256 canonical serialization:**
```python
import hashlib, json

def config_hash(config: dict) -> str:
    canonical = json.dumps(config, sort_keys=True, ensure_ascii=True)
    return "sha256:" + hashlib.sha256(canonical.encode()).hexdigest()
```

---

## 5. When to Recommend TERMINATE_SUCCESS

The Supervisory Controller should recommend `TERMINATE_SUCCESS` (via the Refiner Agent's `STOP` signal) when:

| Condition | Rationale |
|---|---|
| First iteration is OPTIMAL + all checks pass | No refinement needed; solution is already good |
| Plateau detected (last 3 < 0.1%) | Diminishing returns; more iterations won't help |
| Best feasible within 2% of LP relaxation | Near-optimal by bound quality argument |
| All policy constraints met with meaningful slack | Problem is well-solved; no binding constraint to relax |

**Single-iteration termination is the norm in the MVP**, because the default scenario is well-configured and the mock solver produces a clean OPTIMAL result on the first try. Multiple iterations are needed only when the scenario has tight constraints or infeasibility.

---

## 6. When NOT to Terminate

Do NOT terminate early when:
- The scenario has a tight emission cap (< 20% slack) — there may be a better solution
- The MIP gap was > 1% at termination (solution may be far from optimal)
- Infeasibility was resolved by relaxation — the first feasible solution may be over-relaxed
- The improvement trend is monotonically decreasing but still > 0.1% — let it continue

---

## 7. Cross-Session Memory

**What to retain across sessions (indefinitely):**
- All failure patterns (prevent repeating known-bad configurations)
- The top-5 best feasible solutions (provide warm-start seeds for similar future problems)
- Structural infeasibility diagnoses (e.g., "fleet without gas CT cannot meet ERCOT peak + reserves")

**What to prune (after 7 days):**
- Intermediate feasible solutions that are not in the top-5
- Convergence trend entries older than 7 days from non-best sessions

**Why 7 days?** Power system planning problems typically have 1–5 day feedback loops. After 7 days, the scenario configuration is likely to have changed sufficiently that old intermediate solutions are no longer useful as warm starts.

---

## 8. JSONL Append-Only Design

The `run_history.jsonl` file is an append-only log: one JSON object per line, one line per completed run. Benefits:
- **Atomic writes**: each line write is atomic; no partial file corruption on crash
- **Easy streaming**: `tail -n 20 run_history.jsonl` shows recent runs without loading the full file
- **Simple replay**: re-process from scratch by reading lines sequentially

The full structured memory (`memory/session_{id}.json`) is rebuilt from the JSONL on demand. If the JSON file is corrupted, it can always be reconstructed from the JSONL.
