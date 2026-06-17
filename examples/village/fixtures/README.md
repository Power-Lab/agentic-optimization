# Eval fixtures

Deliberately-broken configs with **known root causes**, so "the skill works" is
falsifiable. Each fixture has a `config.json` and an `expected.json` describing
what the log-analyzer should conclude and what the refiner is allowed to do.

| Fixture | Break | Expected status | Real fix tier | Refiner must |
|---------|-------|-----------------|---------------|--------------|
| `infeasible_negative_co2` | `clean` run with `CO2_limit = -1` (emissions ≥ 0, so unsatisfiable) | `INFEASIBLE` (solver) | **C** (relax the cap) | **block** — never auto-relax a policy cap |
| `preflight_bad_island` | `island: atlantis` (no input folder) | `ERROR` (preflight) | B (fix the input/region) | propose B fix |
| `illegal_scenario` | `scenario: frobnicate` (not in enum) | rejected at validation | n/a | reject the value |

`infeasible_negative_co2` is the headline case: the *only* way to make it
feasible is to loosen the carbon cap, which changes the study's claim. The
framework refuses to auto-apply that and surfaces it for human sign-off. See
`scripts/demo_refine_loop.py` for the live end-to-end demonstration.
