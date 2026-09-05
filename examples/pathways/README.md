# Pathways adapter — setup and usage

`adapters/pathways/` drives **models/pathways** (the China renewable-energy
Pathways model, `Power-Lab/AdvAppliedEnergy_Pathways_2025`, pinned `99f94de`):
a provincial-resolution, hourly capacity-expansion + dispatch LP for China's
power sector, solved with Gurobi.

It is the framework's second *real* model. It shares nothing with the reference
adapter except the `Adapter` contract — different language conventions,
different failure modes, and **no config file at all** — which is what makes the
claim "the framework is model-agnostic" testable rather than asserted.

> Nothing in this directory was executed in the build that wrote it. Pathways
> needs a Gurobi licence and a ~10 GB data set. `smoke_test.py` is the
> acceptance test; a first green run of it is the real sign-off.

## 1. Prerequisites

| What | Why | How |
|---|---|---|
| The submodule | the model source | `git submodule update --init models/pathways` |
| A Python 3.9/3.10 env with `gurobipy`, `pandas`, `numpy`, `scipy`, `geopandas`, `shapely` | the model's imports | `conda create -n agentic-pathways python=3.10 && conda install -c conda-forge pandas numpy scipy geopandas shapely && pip install gurobipy` |
| A Gurobi licence | `main.py:346` builds a `gurobipy.Model` directly; there is no open-solver path | `~/gurobi.lic` or `GRB_LICENSE_FILE` |
| The Zenodo data set | `data_pkl/`, `data_mat/`, `data_shp/` — gridded VRE capacity factors, provincial demand layers, county shapefiles | see below |

The data is **not** in the repository and is not reproducible from it. Download
the archive that accompanies the paper and unpack it so that the three folders
sit side by side:

```
$PATHWAYS_DATA_ROOT/
├── data_mat/     # RegionDemand_Rev2.mat, levels_*.mat, onshore2015/, solar2015/, …
├── data_pkl/     # province_loc_by_eco.pkl, {wind,solar}_cell_2015.pkl, …
└── data_shp/     # re_county_level.shp (+ .dbf/.shx/.prj)
```

`PathwaysAdapter` also accepts an archive that unpacked one level deep
(`<root>/<name>/data_pkl`) and resolves it for you.

```bash
export PATHWAYS_DATA_ROOT=$HOME/Documents/model-data/AdvAppliedEnergy_Pathways_2025
export PATHWAYS_PYTHON=$HOME/miniforge3/envs/agentic-pathways/bin/python
```

Both have sensible fallbacks: `PATHWAYS_PYTHON` defaults to
`~/miniforge3/envs/agentic-pathways/bin/python` when it exists, then `python3`.
`PATHWAYS_DATA_ROOT` has no fallback — without it `validate_config` fails and
nothing is launched.

## 2. How the adapter drives a model with no config file

The model's scripts mutate a `scen_params` dict in Python, and
`callUtility.getWorkDir` derives *every* input and output path from where
`pycode/` sits — `abspath(dirname(dirname(callUtility.__file__)))`, which does
not resolve symlinks. The adapter turns that into a feature. For each run it
builds a workspace:

```
<run_dir>/
├── config.json            # the executed config (res_tag injected)
├── solver.log             # the driver's stdout, teed line by line
├── driver_result.json     # status, reason, stage, results dir, timings
├── outputs/               # archived result CSVs, year stripped from the names
└── workspace/             # work_dir, as the model sees it
    ├── pycode      -> models/pathways/pycode          (symlink)
    ├── data_pkl    -> $PATHWAYS_DATA_ROOT/data_pkl    (symlink)
    ├── data_mat    -> …                               (symlink)
    ├── data_shp    -> …                               (symlink)
    ├── data_csv/   # real dir, one symlink per entry of the model's data_csv,
    │               # except capacity_assumptions/ which is a real dir of file
    │               # symlinks so one CSV can be replaced (the emission override)
    ├── data_res/   # real dir — every model output lands here
    └── gurobi.env  # TimeLimit / Threads / BarConvTol / NumericFocus / LogFile
```

Importing the model through `workspace/pycode` makes `work_dir` the workspace,
so the model reads shared inputs and writes only inside the run directory. The
model is never edited, and concurrent runs cannot collide.

`adapters/pathways/driver.py` runs inside `$PATHWAYS_PYTHON` and replays
`pycode/testSingleYear.py`'s flow (`automate_inputs` → `seedHour` →
`initDemLayer` → `initCellData` ×2 → `initModelExovar` → `interProvinModel` →
post-processing), printing `[pathways] stage: …` / `[pathways] status: …`
markers. It exits 0 for INFEASIBLE and TIME_LIMIT: a failed solve is a result,
not a crash.

## 3. Run one

```python
from adapters.pathways import PathwaysAdapter, SMOKE_CONFIG
from framework import run_and_record

adapter = PathwaysAdapter()                       # env-configured
print(adapter.validate_config(SMOKE_CONFIG))
record = run_and_record(adapter, SMOKE_CONFIG, "runs/pathways_smoke")
print(record.execution.termination_status)
print(adapter.locate_outputs("runs/pathways_smoke"))
```

or from the shell:

```bash
python examples/pathways/smoke_test.py --dry-run     # validate, print the command
python examples/pathways/smoke_test.py               # solve (minutes)
```

`adapter.cleanup_workspace(run_dir)` deletes `workspace/` once the outputs are
archived — the symlinks are free but the model's per-province hourly CSVs under
`data_res/` are real and add up across a sweep.

## 4. Config and tiers

`adapter.describe_config()` prints the full schema with a `models/pathways`
citation for every key; that string is also what the skills read. The short
version:

- **Tier A** (auto-apply, numerics + label): `time_limit`, `threads`,
  `bar_conv_tol`, `numeric_focus`, `res_tag`. These become Gurobi parameters in
  `gurobi.env`. `Method` and `Crossover` are deliberately *not* exposed — the
  model sets them in code (`main.py:347-348`) and would override anything here.
- **Tier B** (auto-apply + flag): `year`, `optimization_days`,
  `optimization_step`, `vre_year`, `demand_scale`, `demand_sensitivity`,
  `renewable_cost_decline`, `heating_electrification`,
  `endogenize_firm_capacity`, `ccs_retrofit_cost`, `wacc`.
- **Tier C** (human sign-off): `emission_target`, `emission_cap_override_mt`,
  `comply_with_medium_vre_goal`, `ccs_start_year`, `with_shedding`,
  `demand_resv`, `vre_resv`, `wind_with_xz`.

`with_shedding` is the one to watch. Turning it on lets the model drop load
(`main.py:414, 1010, 1030, 1125`), which makes almost any infeasible case
"solve" — the archetypal silent relaxation, and why it is Tier C.

Two config keys do something the model does not do by itself, and both are
implemented in the per-run workspace rather than by editing the model:

- `emission_cap_override_mt` writes a workspace copy of
  `data_csv/capacity_assumptions/power_sector_emission_<target>.csv` with the
  row for `year` replaced. Beware the clamp: a negative cap becomes exactly 0
  when `ccs_start_year > year` (`main.py:1334-1336`).
- `demand_scale` rewrites the workspace's `provin_demand_hourly/*.csv`.
  `initDemLayer` reads `scen_params["demand"]["scale"]` into `alpha`
  (`initData.py:949`) but the block that used `alpha` is commented out
  (`initData.py:1006-1013`), so setting the scen_params key alone is a no-op in
  the model as pinned. The adapter sets both, so the provenance is visible.

## 5. Cost, and the remote backend

A single year at 5 sampled days is the smallest useful run. The model's own
README puts the 1-week-per-decade multi-year demo at ~30 min on 10 cores, and a
full 8760-hour year at up to 20 h on 32 cores. For anything beyond a few
representative days, use `RemotePathwaysAdapter`, which ships the config, the
driver and the shim to a node, runs them there and pulls the outputs back:

```python
from adapters.pathways import RemotePathwaysAdapter

adapter = RemotePathwaysAdapter(
    ssh_host="pwrlab",
    remote_root="~/pathways-jobs",
    remote_model_root="~/AdvAppliedEnergy_Pathways_2025",
    remote_data_root="~/pathways-data",
    python="~/miniforge3/envs/pathways/bin/python",
)
print(adapter.check_connection())
```

It is **untested against a live node**; only its orchestration is covered by
`tests/test_pathways_adapter.py` (with an in-memory transport).

## 6. Fixtures

`fixtures/` holds five labelled cases — one clean baseline plus one per failure
family — with the protocol's §5 label schema. See `fixtures/README.md`. Every
solver-backed one needs Gurobi; `preflight_missing_data` needs nothing and runs
anywhere.

## 7. Troubleshooting

| Symptom | Cause |
|---|---|
| `PATHWAYS_DATA_ROOT is not set` | export it, or pass `data_root=` to the adapter |
| `data directory not found under …: data_pkl, …` | the archive unpacked somewhere else, or more than one level deep |
| `work_dir mismatch … aborting before any write` | the driver refused to run because the model resolved a `work_dir` outside the workspace — do not "fix" this by loosening the check; it is what stops the model writing into the submodule |
| `import: ModuleNotFoundError: gurobipy` | `PATHWAYS_PYTHON` points at the wrong interpreter |
| `FileExistsError` inside `automate_inputs` | the workspace was reused; the driver always builds a fresh one, so this means `<run_dir>/workspace` was pre-populated by hand |
| status `UNKNOWN` | the driver exited 0 without a status marker — read `solver.log`, then `workspace/gurobi.log` |
