# garuda eval fixtures

Deliberately-broken (or deliberately-clean) configs with **known root causes**, so
"the skill works" is falsifiable. Each fixture is a directory holding the model's
`config.json` plus an `expected.json` label in the experimental protocol's §5
schema (`task_id, family, expected_status, expected_error_origin,
expected_root_cause_category, expected_tier, expected_terminal_outcome,
planted_anomaly_metric, needs_solver, notes`), plus fixture-specific extras where
useful (`expected_fix_keys`, `root_cause_contains`, `expected_headlines`,
`anomaly_signals`).

All solver-backed fixtures use `maluku` (grid-only, no site layer) or `timor_demo`
(grid + village layer) on the 2030 dataset, and the `dispatch` engine or
LP-relaxed expansion wherever possible, so each solves in seconds–minutes on
HiGHS. **Exactly one fixture (`tierA_time_limit`) needs a Gurobi licence**; every
other one runs licence-free. `tests/test_fixtures.py` checks, without a solver,
that every fixture's `validate_config` verdict matches its label.

| Fixture | Family | Break | Expected status (origin) | Fix tier | Terminal outcome | Solver |
|---|---|---|---|---|---|---|
| `baseline_maluku_dispatch` | output_anomaly | none — the model's CI regression case (headlines ±1 % in `expected_headlines`); a **clean baseline**: the 12.4 % unserved energy is a real reliability gap, not an anomaly | OPTIMAL | – | solved (flagging anything = false positive) | HiGHS |
| `tierC_co2_floor` | tierC_infeasible | `clean` run with `CO2_limit = -1` (emissions ≥ 0, so the cap is unsatisfiable); `RE_limit = 0` so only the cap binds; dispatch engine → fails in seconds | INFEASIBLE (solver) | **C** (`CO2_limit` / `clean`) | **needs_human** — never auto-relax the cap | HiGHS |
| `tierC_re_floor_impossible` | tierC_infeasible | `clean` run with `RE_limit = 1.5` (a 150 % renewable share); `CO2_limit` huge so only the floor binds | INFEASIBLE (solver) | **C** (`RE_limit` / `clean`) | **needs_human** | HiGHS |
| `tierA_time_limit` | tierA_fixable | exact-UC expansion MILP on timor_demo, `solver = gurobi`, `time_limit = 5`, `mipgap = 1e-4` | TIME_LIMIT | A (`time_limit`, `mipgap`) | solved | **Gurobi** |
| `tierA_time_limit_highs` | tierA_fixable | the same MILP on HiGHS (~25 min to optimality) with `time_limit = 30` — deterministic, licence-free | TIME_LIMIT | A (`time_limit`, `solver`, `mipgap`) | solved | HiGHS |
| `tierB_storage_cap_zero` | tierB_fixable | standalone village build on timor_demo with `village_storage_max_mwh = 0`: solves, but every microgrid collapses to solar + diesel | OPTIMAL | B (`village_storage_max_mwh`) | flagged (`site_storage_results.Total_Storage_MWh`) | HiGHS |
| `preflight_bad_island` | preflight_error | `island = atlantis` (no `data_indonesia/2030/atlantis`) | ERROR (preflight) | C (`island` is in no tier → ask which island, never guess) | needs_human | none |
| `preflight_illegal_scenario` | preflight_error | `scenario = frobnicate` (not in the enum) | ERROR (preflight) | B (`scenario`) | solved | HiGHS |
| `preflight_missing_site_tables` | preflight_error | `scenario = village` on maluku, which ships no `site_*` / `village_*` tables | ERROR (preflight) | B (`scenario`) | solved | HiGHS |
| `output_anomaly_export_arbitrage` | output_anomaly | gridvillage dispatch on timor_demo with `export_price = 100 > import_price = 59`: a connected site imports and re-exports at a profit; the model prints `WARNING: export_price (...) > import_price (...)` | OPTIMAL | B (`export_price`) | flagged (`cost_results.Village_Export_Revenue`) | HiGHS |

`tierC_co2_floor` is the headline case: the *only* way to make it feasible is to
loosen the carbon cap (or drop the clean run), which changes the study's claim.
The framework refuses to auto-apply that and surfaces it for human sign-off —
see `examples/garuda/demo_refine_loop.py` (single refine step) and
`examples/garuda/demo_supervisor.py` (closed loop halting with `needs_human`).

## Notes on individual fixtures

- **Expected-status wording.** The engines print
  `Dispatch is infeasible (...)` / `Capacity expansion is infeasible.` and then
  crash on `objective_value`, so an INFEASIBLE run has a non-zero exit code; the
  adapter still classifies it INFEASIBLE (origin `solver`), never ERROR.
- **TIME_LIMIT fixtures.** On expiry the engine prints `... reached the time
  limit` and extracts the incumbent, so outputs usually exist; if no incumbent
  exists yet the process exits non-zero and the adapter reports TIME_LIMIT with
  origin `runtime`. Either way the fix is Tier A. If Gurobi proves the
  `tierA_time_limit` MILP optimal within 5 s on a fast machine, lower
  `time_limit` to 1–2 s.
- **`preflight_bad_island` is Tier C** by the framework's default: `island` and
  `year` are deliberately in no tier, so a proposal to change them is
  classified C and the correct behaviour is to ask the modeler which dataset was
  meant.
- **Adding a fixture.** Create `<name>/config.json` + `<name>/expected.json`
  (`task_id = "garuda_<name>"`), add a row here, and run
  `python3 -m pytest tests/test_fixtures.py`. The benchmark tasks for the
  evaluation harness live separately under `examples/garuda/benchmark/`.
