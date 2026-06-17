---
name: output-analyzer
description: Inspect a solved run's result outputs for implausible or unexpected patterns (dispatch, capacity, cost, emissions), optionally comparing against a baseline run, and record anomalies into run_record.json. Use after an OPTIMAL run to sanity-check results, when the user asks "are these results realistic?" or "why are emissions/costs so high?", or as the analyze step for a successful solve. Model-agnostic.
---

# Output Analyzer (Task 4)

Read a solved run's outputs, flag anomalies, and record them. You analyze; you do
not change the config. Pairs with `log-analyzer` (which handles *failed* runs) —
this one handles *successful* runs whose results may still be wrong.

## Operating rules

1. **Load outputs through the adapter; never hard-code filenames.**
   ```python
   from framework import get_adapter, read_outputs, record_anomalies, RunRecord, Anomaly
   adapter = get_adapter()
   outputs = read_outputs(adapter, run_dir)   # {output_name: [row dicts]}
   ```
   `get_adapter().describe_config()` and the model's outputs documentation tell
   you what each output means and the sensible ranges.

2. **Look for the classic failure signatures**, scaled to the active model:
   - non-served energy / unmet demand above a tolerance (reliability),
   - a cost or emissions component orders of magnitude off expectation,
   - a capacity/dispatch result that is physically implausible (e.g. all-thermal
     where renewables are cheap, or zero build where build is forced),
   - results that barely differ from a baseline that *should* differ.

3. **Compare to a baseline when one exists.** Load the baseline run's
   `run_record.json` / outputs and diff the headline metrics; flag deltas that
   are too large or suspiciously zero.

4. **Record anomalies** (each: metric, value, expected, severity):
   ```python
   rec = RunRecord.load(run_dir / "run_record.json")
   record_anomalies(rec, [Anomaly(metric="Total_NSE_MWh", value=..., expected="~0", severity="high")], run_dir)
   ```

## Output

List each anomaly with metric, observed value, expected range, and severity, and
a one-line interpretation. If everything looks plausible, say so explicitly — a
clean bill is a valid, useful result. Anomalies that imply a *config* problem can
be handed to the `refiner`; anomalies that imply a *data/model* problem should be
surfaced to the human.
