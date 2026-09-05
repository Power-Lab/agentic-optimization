"""Solver-free tests for the pathways adapter (the config-injection shim).

The model itself is never imported here: it needs Python 3.9/3.10, pandas,
geopandas, gurobipy and a ~10 GB data set, none of which the test interpreter
has. Everything the adapter actually owns is pure and therefore testable:

* the flat config schema and its validation,
* ``config -> scen_params`` injection (against a synthetic template *and*, when
  the submodule is checked out, against the model's real template, so template
  drift is caught),
* the per-run workspace builder — symlink layout, the materialised
  ``capacity_assumptions/`` directory, and the guarantee that an emission-cap
  override never writes through a symlink into ``models/``,
* Gurobi log / driver marker parsing,
* output archiving and the tier declaration,
* ``run()`` end to end against a *stub driver*, so streaming, status parsing,
  ``res_tag`` injection and archiving are exercised without a solver.

Nothing here needs pandas, so it runs on the bare homebrew interpreter.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from adapters.pathways import PathwaysAdapter, shim
from framework.interventions import Tier

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "examples" / "pathways" / "fixtures"
MODEL_ROOT = ROOT / "models" / "pathways"
DRIVER = ROOT / "adapters" / "pathways" / "driver.py"


# ---------------------------------------------------------------------------
# fake trees
# ---------------------------------------------------------------------------

#: The parts of scen_params_template.json that build_scen_params writes into.
#: Deliberately minimal: if the mapping starts touching a new key, this template
#: no longer covers it and ``test_real_template_has_every_injected_path`` says so.
SYNTHETIC_TEMPLATE = {
    "scenario": {"comply_with_medium_vre_goal": 0, "endogenize_firm_capacity": 1,
                 "ccs_start_year": 2040, "emission_target": "2C",
                 "heating_electrification": "chp_ccs", "emission_factor_method": "mean",
                 "renewable_cost_decline": "baseline", "demand_sensitivity": "+5"},
    "optimization_hours": {"years": 0, "step": 10, "days": 5},
    "finance": {"weighted_average_cost_of_capital": 7.4},
    "storage": {"capex_power_phs": 3840, "capex_power_bat": 2700,
                "capex_power_lds": {"caes": 4800, "vrb": 3000}},
    "vre": {"capex_equip_on_wind": 2200, "capex_om_on_wind": 45,
            "capex_equip_off_wind": 3800, "capex_om_off_wind": 81,
            "capex_equip_pv": 1100, "capex_om_pv": 7.5,
            "capex_equip_dpv": 1400, "capex_om_dpv": 10, "wind_with_xz": 0},
    "ccs": {"capex_coal_ccs": 3500, "capex_gas_ccs": 3500},
    "demand": {"scale": 1},
    "resv": {"vre_resv": 0.05, "demand_resv": 0.05},
    "shedding": {"with_shedding": 0, "shedding_vom": 2},
}

EMISSION_CSV = "Year,CO2_emission\n2030,3900.56602\n2040,1400.893784\n2060,-563.4711311"


def fake_model(tmp_path: Path) -> Path:
    """A model tree with just the files the adapter and workspace builder touch."""
    root = tmp_path / "model"
    (root / "pycode").mkdir(parents=True)
    (root / "pycode" / "main.py").write_text("# fake\n")
    (root / "pycode" / "callUtility.py").write_text("# fake\n")
    csv_dir = root / "data_csv"
    (csv_dir / "capacity_assumptions").mkdir(parents=True)
    (csv_dir / "geography").mkdir()
    (csv_dir / "scen_params_template.json").write_text(json.dumps(SYNTHETIC_TEMPLATE))
    (csv_dir / "loose.csv").write_text("a,b\n1,2\n")
    (csv_dir / "geography" / "provinces.csv").write_text("provin\nAnhui\n")
    for target in ("2C", "15C"):
        (csv_dir / "capacity_assumptions" / f"power_sector_emission_{target}.csv").write_text(
            EMISSION_CSV)
    (csv_dir / "capacity_assumptions" / "coal_2020.csv").write_text("province,cap\n")
    return root


def fake_data(tmp_path: Path, name: str = "data") -> Path:
    root = tmp_path / name
    for d in shim.DATA_DIRS:
        (root / d).mkdir(parents=True)
    (root / "data_pkl" / "province_loc_by_eco.pkl").write_bytes(b"\x80\x04.")
    return root


@pytest.fixture
def adapter(tmp_path):
    return PathwaysAdapter(model_root=fake_model(tmp_path), data_root=fake_data(tmp_path),
                           python=sys.executable)


# ---------------------------------------------------------------------------
# schema / validation
# ---------------------------------------------------------------------------

def test_minimal_config_validates(adapter):
    assert adapter.validate_config({"year": 2060}).ok


def test_year_is_required(adapter):
    errors = adapter.validate_config({"optimization_days": 3}).errors
    assert any("Missing required config keys: year" in e for e in errors)


def test_unknown_keys_are_rejected(adapter):
    errors = adapter.validate_config({"year": 2060, "co2_cap_t": 0}).errors
    assert any("Unknown config keys: co2_cap_t" in e for e in errors)


@pytest.mark.parametrize("key,bad", [
    ("year", 2035),
    ("emission_target", "3C"),
    ("ccs_start_year", 2035),
    ("heating_electrification", "resistive"),
    ("renewable_cost_decline", "aggressive"),
    ("demand_sensitivity", "p10"),
    ("with_shedding", 2),
    ("numeric_focus", 4),
    ("vre_year", "w2019_s2019"),
])
def test_enums_are_enforced(adapter, key, bad):
    errors = adapter.validate_config({"year": 2060, key: bad}).errors
    assert any(f"for {key!r}" in e for e in errors), errors


@pytest.mark.parametrize("config,fragment", [
    ({"year": 2060, "optimization_days": 2.5}, "must be an integer"),
    ({"year": 2060, "optimization_days": 0}, "must be >= 1"),
    ({"year": 2060, "demand_scale": -1}, "must be a number > 0"),
    ({"year": 2060, "time_limit": 0}, "must be a number > 0"),
    ({"year": 2060, "wacc": -0.1}, "must be a number >= 0"),
    ({"year": 2060, "emission_cap_override_mt": "lots"}, "must be a number"),
    ({"year": 2060, "res_tag": 7}, "must be a string"),
])
def test_type_and_range_errors(adapter, config, fragment):
    errors = adapter.validate_config(config).errors
    assert any(fragment in e for e in errors), errors


def test_horizon_must_fit_in_one_year(adapter):
    """seedHour drops any hour past 8759 without complaining (initData.py:65-72),
    so a horizon that overflows the year must be refused, not silently truncated."""
    errors = adapter.validate_config(
        {"year": 2060, "optimization_days": 40, "optimization_step": 10}).errors
    assert any("exceeds one year" in e for e in errors), errors
    assert adapter.validate_config(
        {"year": 2060, "optimization_days": 37, "optimization_step": 10}).ok


def test_missing_data_root_is_a_validation_error(tmp_path, monkeypatch):
    monkeypatch.delenv("PATHWAYS_DATA_ROOT", raising=False)
    a = PathwaysAdapter(model_root=fake_model(tmp_path), data_root=None,
                        python=sys.executable)
    result = a.validate_config({"year": 2060})
    assert not result.ok
    assert any("PATHWAYS_DATA_ROOT is not set" in e for e in result.errors)


def test_nonexistent_data_root_is_a_validation_error(tmp_path):
    a = PathwaysAdapter(model_root=fake_model(tmp_path),
                        data_root=tmp_path / "nope", python=sys.executable)
    errors = a.validate_config({"year": 2060}).errors
    assert any("data directory not found" in e for e in errors), errors


def test_missing_model_root_is_a_validation_error(tmp_path):
    a = PathwaysAdapter(model_root=tmp_path / "gone", data_root=fake_data(tmp_path),
                        python=sys.executable)
    errors = a.validate_config({"year": 2060}).errors
    assert any("model file not found" in e for e in errors), errors


def test_data_root_nested_one_level_is_resolved(tmp_path):
    """The Zenodo archive commonly unpacks into <root>/<name>/data_pkl."""
    outer = tmp_path / "outer"
    outer.mkdir()
    fake_data(outer, "AdvAppliedEnergy_Pathways_2025")
    assert shim.check_data_root(outer) == []


# ---------------------------------------------------------------------------
# config -> scen_params
# ---------------------------------------------------------------------------

def test_scen_params_defaults_reproduce_the_template():
    """A config carrying only ``year`` must not move any policy dial."""
    sp = shim.build_scen_params(SYNTHETIC_TEMPLATE, {"year": 2060})
    for key in ("comply_with_medium_vre_goal", "endogenize_firm_capacity",
                "ccs_start_year", "emission_target", "heating_electrification"):
        assert sp["scenario"][key] == SYNTHETIC_TEMPLATE["scenario"][key]
    assert sp["shedding"]["with_shedding"] == 0
    assert sp["resv"]["demand_resv"] == 0.05
    assert sp["vre"]["wind_with_xz"] == 0
    assert sp["optimization_hours"]["days"] == 5
    assert sp["optimization_hours"]["step"] == 10


def test_build_scen_params_does_not_mutate_the_template():
    before = json.dumps(SYNTHETIC_TEMPLATE, sort_keys=True)
    shim.build_scen_params(SYNTHETIC_TEMPLATE, {"year": 2030, "with_shedding": 1,
                                                "demand_scale": 2.0})
    assert json.dumps(SYNTHETIC_TEMPLATE, sort_keys=True) == before


def test_every_config_key_lands_somewhere_in_scen_params():
    """Each Tier-B/C key that the model consumes through scen_params must move a
    value; a key that quietly does nothing is worse than no key."""
    config = {
        "year": 2060, "comply_with_medium_vre_goal": 1, "endogenize_firm_capacity": 0,
        "ccs_start_year": 2050, "emission_target": "15C",
        "heating_electrification": "heat_pump", "renewable_cost_decline": "conservative",
        "demand_sensitivity": "m5", "ccs_retrofit_cost": 4321, "wacc": 6.0,
        "demand_scale": 2.0, "with_shedding": 1, "demand_resv": 0.11,
        "vre_resv": 0.12, "wind_with_xz": 1, "optimization_days": 7,
        "optimization_step": 20,
    }
    sp = shim.build_scen_params(SYNTHETIC_TEMPLATE, config)
    assert sp["scenario"]["comply_with_medium_vre_goal"] == 1
    assert sp["scenario"]["endogenize_firm_capacity"] == 0
    assert sp["scenario"]["ccs_start_year"] == 2050
    assert sp["scenario"]["emission_target"] == "15C"
    assert sp["scenario"]["heating_electrification"] == "heat_pump"
    assert sp["scenario"]["renewable_cost_decline"] == "conservative"
    assert sp["scenario"]["demand_sensitivity"] == "m5"
    assert sp["ccs"]["capex_coal_ccs"] == 4321 and sp["ccs"]["capex_gas_ccs"] == 4321
    assert sp["finance"]["weighted_average_cost_of_capital"] == 6.0
    assert sp["demand"]["scale"] == 2.0
    assert sp["shedding"]["with_shedding"] == 1
    assert sp["resv"]["demand_resv"] == 0.11 and sp["resv"]["vre_resv"] == 0.12
    assert sp["vre"]["wind_with_xz"] == 1
    assert sp["optimization_hours"]["days"] == 7
    assert sp["optimization_hours"]["step"] == 20


def test_booleans_are_coerced_for_binary_keys():
    sp = shim.build_scen_params(SYNTHETIC_TEMPLATE,
                                {"year": 2060, "with_shedding": True, "wind_with_xz": False})
    assert sp["shedding"]["with_shedding"] == 1
    assert sp["vre"]["wind_with_xz"] == 0


def test_cost_trajectory_matches_the_model_arithmetic():
    """testMultiYear.py:112-148 reads index idx+1 of a linspace with
    len(yr_req)+1 = 9 points; 2060 is the last requested year, so it lands on the
    end point (7700 -> 3000, minus the 800 'other' component)."""
    traj = shim.cost_trajectory(2060, "baseline")
    assert traj["capex_equip_on_wind"] == pytest.approx(3000 - 800)
    assert traj["capex_equip_pv"] == pytest.approx(1500 - 400)
    assert traj["capex_om_on_wind"] == pytest.approx(45)
    assert traj["capex_power_bat"] == pytest.approx(2700)
    # 2025 is index 0, so it reads element 1 of the 9-point trajectory.
    early = shim.cost_trajectory(2030, "baseline")
    assert early["capex_equip_on_wind"] > traj["capex_equip_on_wind"]
    conservative = shim.cost_trajectory(2060, "conservative")
    assert conservative["capex_equip_pv"] == pytest.approx(2656.2 - 400)
    assert conservative["capex_equip_pv"] > traj["capex_equip_pv"]


def test_cost_trajectory_flows_into_scen_params():
    sp = shim.build_scen_params(SYNTHETIC_TEMPLATE,
                                {"year": 2060, "renewable_cost_decline": "conservative"})
    assert sp["vre"]["capex_equip_pv"] == pytest.approx(2656.2 - 400)
    assert sp["storage"]["capex_power_lds"]["caes"] == pytest.approx(12000)


@pytest.mark.skipif(not (MODEL_ROOT / "data_csv" / "scen_params_template.json").is_file(),
                    reason="models/pathways submodule not checked out")
def test_real_template_has_every_injected_path():
    """Guards against template drift in the pinned model: every path
    build_scen_params writes must already exist in the model's own template."""
    template = json.loads(
        (MODEL_ROOT / "data_csv" / "scen_params_template.json").read_text())
    config = {"year": 2060, "with_shedding": 1, "demand_scale": 2.0, "wacc": 6.0,
              "ccs_retrofit_cost": 4321, "renewable_cost_decline": "conservative"}
    sp = shim.build_scen_params(template, config)
    assert sp["shedding"]["with_shedding"] == 1
    assert sp["demand"]["scale"] == 2.0
    assert sp["storage"]["capex_power_lds"]["caes"] == pytest.approx(12000)
    # No new top-level section was invented.
    assert set(sp) == set(template)
    for section, body in sp.items():
        if isinstance(body, dict):
            assert set(body) == set(template[section]), section


# ---------------------------------------------------------------------------
# workspace builder
# ---------------------------------------------------------------------------

def test_workspace_layout(tmp_path):
    model, data = fake_model(tmp_path), fake_data(tmp_path)
    ws = shim.build_workspace(tmp_path / "run" / "workspace", model, data)

    # pycode is a symlink: getWorkDir uses abspath (no symlink resolution), so
    # work_dir becomes the workspace and every model path relocates with it.
    assert ws.pycode.is_symlink()
    assert ws.pycode.resolve() == (model / "pycode").resolve()
    for d in shim.DATA_DIRS:
        link = ws.root / d
        assert link.is_symlink() and link.resolve() == (data / d).resolve()

    # data_csv is a real directory of per-entry symlinks...
    assert ws.data_csv.is_dir() and not ws.data_csv.is_symlink()
    assert (ws.data_csv / "loose.csv").is_symlink()
    assert (ws.data_csv / "geography").is_symlink()
    # ...except capacity_assumptions, which is materialised so one CSV inside it
    # can be replaced by a real file for the emission-cap override.
    caps = ws.data_csv / "capacity_assumptions"
    assert caps.is_dir() and not caps.is_symlink()
    assert (caps / "coal_2020.csv").is_symlink()

    # data_res is real: every model output lands inside the run directory.
    assert ws.data_res.is_dir() and not ws.data_res.is_symlink()
    assert ws.gurobi_env is None


def test_workspace_is_rebuilt_from_scratch(tmp_path):
    model, data = fake_model(tmp_path), fake_data(tmp_path)
    root = tmp_path / "run" / "workspace"
    shim.build_workspace(root, model, data)
    (root / "data_res" / "stale").mkdir()
    shim.build_workspace(root, model, data)
    assert not (root / "data_res" / "stale").exists()


def test_workspace_refuses_a_missing_model(tmp_path):
    with pytest.raises(shim.WorkspaceError, match="model file not found"):
        shim.build_workspace(tmp_path / "ws", tmp_path / "absent", fake_data(tmp_path))


def test_workspace_refuses_missing_data(tmp_path):
    with pytest.raises(shim.WorkspaceError, match="data directory not found"):
        shim.build_workspace(tmp_path / "ws", fake_model(tmp_path), tmp_path / "absent")


def test_gurobi_env_is_written_into_the_workspace(tmp_path):
    model, data = fake_model(tmp_path), fake_data(tmp_path)
    params = shim.gurobi_params({"time_limit": 10, "threads": 4, "numeric_focus": 2},
                                log_file="/tmp/g.log")
    ws = shim.build_workspace(tmp_path / "ws", model, data, gurobi_params=params)
    text = ws.gurobi_env.read_text()
    assert "TimeLimit 10" in text and "Threads 4" in text and "NumericFocus 2" in text
    assert "LogFile /tmp/g.log" in text
    # main.py:347-348 sets these in code; exposing them would be a lie.
    assert "Method" not in text and "Crossover" not in text


def test_gurobi_params_only_include_what_the_config_sets():
    assert shim.gurobi_params({"year": 2060}) == {}
    assert shim.gurobi_params({"bar_conv_tol": 1e-6}) == {"BarConvTol": 1e-6}


# ---------------------------------------------------------------------------
# emission-cap override
# ---------------------------------------------------------------------------

def test_emission_override_replaces_only_the_target_year():
    out = shim.override_emission_csv(EMISSION_CSV, 2060, -1000.0)
    rows = dict(line.split(",") for line in out.strip().splitlines()[1:])
    assert float(rows["2060"]) == -1000.0
    assert rows["2030"] == "3900.56602"
    assert out.splitlines()[0] == "Year,CO2_emission"


def test_emission_override_rejects_an_absent_year():
    with pytest.raises(KeyError):
        shim.override_emission_csv(EMISSION_CSV, 2045, 0.0)


def test_emission_override_never_writes_through_the_symlink(tmp_path):
    """The materialised CSV must *replace* the symlink, not follow it — writing
    through would corrupt the pinned model checkout."""
    model, data = fake_model(tmp_path), fake_data(tmp_path)
    original = (model / "data_csv" / "capacity_assumptions"
                / "power_sector_emission_2C.csv")
    before = original.read_text()
    ws = shim.build_workspace(tmp_path / "ws", model, data,
                              emission_override=("2C", 2060, -1e6))
    materialised = ws.data_csv / "capacity_assumptions" / "power_sector_emission_2C.csv"
    assert materialised.is_file() and not materialised.is_symlink()
    assert "-1e+06" in materialised.read_text() or "-1000000" in materialised.read_text()
    assert original.read_text() == before
    assert ws.overrides and "2060" in ws.overrides[0]


# ---------------------------------------------------------------------------
# demand scaling
# ---------------------------------------------------------------------------

def test_scale_demand_csv_scales_only_the_dem_column():
    text = "hour,dem\n0,10.0\n1,20\n"
    out = shim.scale_demand_csv(text, 2.5)
    rows = [line.split(",") for line in out.strip().splitlines()[1:]]
    assert [r[0] for r in rows] == ["0", "1"]
    assert [float(r[1]) for r in rows] == [25.0, 50.0]
    assert out.splitlines()[0] == "hour,dem"


def test_scale_demand_csv_needs_a_dem_column():
    with pytest.raises(KeyError):
        shim.scale_demand_csv("hour,load\n0,1\n", 2.0)


def test_scale_demand_dir_touches_every_province_and_skips_the_rest(tmp_path):
    folder = tmp_path / shim.DEMAND_DIRNAME
    folder.mkdir()
    (folder / "Anhui.csv").write_text("hour,dem\n0,100\n")
    (folder / "Beijing.csv").write_text("hour,dem\n0,50\n")
    (folder / "nation_dem_full.csv").write_text("0,150\n")  # headerless, no dem column
    touched = shim.scale_demand_dir(folder, 3.0)
    assert touched == ["Anhui.csv", "Beijing.csv"]
    assert float((folder / "Anhui.csv").read_text().splitlines()[1].split(",")[1]) == 300.0
    assert (folder / "nation_dem_full.csv").read_text() == "0,150\n"


def test_scale_demand_dir_is_a_noop_at_factor_one(tmp_path):
    folder = tmp_path / shim.DEMAND_DIRNAME
    folder.mkdir()
    (folder / "Anhui.csv").write_text("hour,dem\n0,100\n")
    assert shim.scale_demand_dir(folder, 1.0) == []
    assert (folder / "Anhui.csv").read_text() == "hour,dem\n0,100\n"


# ---------------------------------------------------------------------------
# Gurobi log / driver output parsing
# ---------------------------------------------------------------------------

OPTIMAL_LOG = """\
Gurobi Optimizer version 10.0.3 build v10.0.3rc0 (mac64[arm])
Optimize a model with 1204553 rows, 903881 columns and 4211002 nonzeros
Barrier statistics:
 Free vars  : 12
 AA' NZ     : 3.211e+06
Barrier solved model in 41 iterations and 612.31 seconds (410.22 work units)
Optimal objective 1.87451233e+07
"""

INFEASIBLE_LOG = """\
Gurobi Optimizer version 10.0.3 build v10.0.3rc0 (mac64[arm])
Presolve removed 88 rows and 12 columns
Solved in 0 iterations and 0.02 seconds (0.00 work units)
Infeasible model
"""

TIME_LIMIT_LOG = """\
Gurobi Optimizer version 10.0.3 build v10.0.3rc0 (mac64[arm])
Barrier performed 9 iterations in 10.01 seconds (8.11 work units)
Time limit reached
"""

INF_OR_UNBD_LOG = "Presolve time: 0.31s\nModel is infeasible or unbounded\n"
NUMERIC_LOG = "Barrier performed 3 iterations\nNumerical trouble encountered\n"


@pytest.mark.parametrize("text,status", [
    (OPTIMAL_LOG, "OPTIMAL"),
    (INFEASIBLE_LOG, "INFEASIBLE"),
    (TIME_LIMIT_LOG, "TIME_LIMIT"),
    (INF_OR_UNBD_LOG, "INFEASIBLE"),
    (NUMERIC_LOG, "ERROR"),
    ("Model is unbounded\n", "ERROR"),
    ("Optimal solution found (tolerance 1.00e-04)\n", "OPTIMAL"),
    ("Work limit reached\n", "TIME_LIMIT"),
])
def test_gurobi_status_patterns(text, status):
    assert shim.parse_gurobi_status(text)[0] == status


def test_no_termination_marker_is_not_a_status():
    assert shim.parse_gurobi_status("Optimize a model with 3 rows\n") == (None, None)


def test_last_termination_marker_wins():
    """Presolve can report 'Solved in 0 iterations' before the real verdict."""
    status, line = shim.parse_gurobi_status(INFEASIBLE_LOG)
    assert status == "INFEASIBLE" and line == "Infeasible model"


def test_a_gurobierror_traceback_cannot_mask_an_infeasible_solve():
    """main.py:1379 reads .objVal with no status check, so an infeasible solve is
    always followed by a traceback on stdout. It must not re-classify the run."""
    text = INFEASIBLE_LOG + textwrap.dedent("""\
        Traceback (most recent call last):
          File "pycode/main.py", line 1379, in interProvinModel
        gurobipy.GurobiError: Unable to retrieve attribute 'objVal'
        [pathways] status: INFEASIBLE
        """)
    assert shim.parse_driver_output(text, 0).status == "INFEASIBLE"


def test_driver_markers_are_parsed():
    text = ("[pathways] stage: workspace\n[pathways] stage: solve\n"
            "[pathways] status: TIME_LIMIT\n[pathways] reason: Gurobi: Time limit reached\n"
            "[pathways] warning: postprocess skipped\n")
    outcome = shim.parse_driver_output(text, 0, gurobi_log=TIME_LIMIT_LOG)
    assert outcome.status == "TIME_LIMIT"
    assert outcome.stage == "solve"
    assert outcome.error_origin == "solver"
    assert outcome.warnings == ["postprocess skipped"]


def test_driver_result_json_is_authoritative():
    outcome = shim.parse_driver_output(
        "", 0, gurobi_log="",
        driver_result={"status": "INFEASIBLE", "stage": "solve",
                       "reason": "Gurobi: Infeasible model",
                       "gurobi_line": "Infeasible model"})
    assert outcome.status == "INFEASIBLE"
    assert outcome.error_origin == "solver"
    assert outcome.gurobi_line == "Infeasible model"


@pytest.mark.parametrize("stage,origin", [
    ("config", "preflight"),
    ("workspace", "preflight"),
    ("import", "preflight"),
    ("inputs", "preflight"),
    ("postprocess", "runtime"),
])
def test_error_origin_follows_the_failing_stage(stage, origin):
    text = f"[pathways] stage: {stage}\n[pathways] status: ERROR\n[pathways] reason: boom\n"
    assert shim.parse_driver_output(text, 1).error_origin == origin


def test_a_solve_stage_error_without_a_gurobi_marker_is_runtime():
    text = "[pathways] stage: solve\n[pathways] status: ERROR\n[pathways] reason: boom\n"
    assert shim.parse_driver_output(text, 1).error_origin == "runtime"


def test_a_crash_without_any_marker_is_an_error():
    outcome = shim.parse_driver_output("Killed\n", 137)
    assert outcome.status == "ERROR" and "137" in outcome.reason


def test_a_silent_success_is_unknown_not_optimal():
    """Exiting 0 with nothing to show must never be read as a solved model."""
    assert shim.parse_driver_output("", 0).status == "UNKNOWN"


# ---------------------------------------------------------------------------
# output archiving
# ---------------------------------------------------------------------------

def write_results_tree(root: Path, year: int = 2060) -> Path:
    outputs = root / "outputs"
    processed = root / "outputs_processed"
    outputs.mkdir(parents=True)
    processed.mkdir(parents=True)
    (outputs / "objValue.csv").write_text("item,million RMB\nobjValue,123.0\n")
    (outputs / "emissionValue.csv").write_text(
        "emission_target_mt,-563.47\nemission_coal_unabated_mt,10.0\n")
    (outputs / "emissionBreakdowns.csv").write_text(",province\n0,Anhui\n")
    (outputs / "shadow_prices").mkdir()  # sub-folders stay behind
    (outputs / "shadow_prices" / "Anhui.csv").write_text("h,p\n")
    (processed / f"summary_national_{year}.csv").write_text("item,value\ndemand_pwh,15.1\n")
    (processed / f"summary_provincial_{year}.csv").write_text("province,demand_twh\n")
    (processed / "trans_cap.csv").write_text("Anhui,Jiangsu,10\n")
    (processed / f"integrated_storage_{year}.csv").write_text("province,phs_total_capacity_mw\n")
    return root


def test_archive_strips_the_year_and_flattens_both_folders(tmp_path):
    src = write_results_tree(tmp_path / "res")
    names = shim.archive_outputs(src, tmp_path / "out", 2060)
    assert names == sorted([
        "objValue.csv", "emissionValue.csv", "emissionBreakdowns.csv",
        "summary_national.csv", "summary_provincial.csv", "trans_cap.csv",
        "integrated_storage.csv",
    ])
    assert (tmp_path / "out" / "summary_national.csv").is_file()
    # Per-province sub-folders are not archived — they are large and per-hour.
    assert not (tmp_path / "out" / "shadow_prices").exists()


def test_archive_of_a_missing_results_tree_is_empty_not_an_error(tmp_path):
    assert shim.archive_outputs(tmp_path / "absent", tmp_path / "out", 2060) == []


def test_locate_outputs_keys_on_the_stem(tmp_path, adapter):
    run_dir = tmp_path / "run"
    (run_dir / "outputs").mkdir(parents=True)
    (run_dir / "outputs" / "objValue.csv").write_text("a,b\n")
    (run_dir / "outputs" / "notes.txt").write_text("ignored")
    assert set(adapter.locate_outputs(run_dir)) == {"objValue"}


def test_locate_outputs_before_a_run_is_empty(tmp_path, adapter):
    assert adapter.locate_outputs(tmp_path / "never-ran") == {}


# ---------------------------------------------------------------------------
# tiers
# ---------------------------------------------------------------------------

def test_tier_declaration_matches_the_approved_split(adapter):
    spec = adapter.intervention_spec()
    assert spec.tier_a_keys == {"time_limit", "threads", "bar_conv_tol",
                                "numeric_focus", "res_tag"}
    assert spec.tier_b_keys == {"renewable_cost_decline", "demand_sensitivity",
                                "demand_scale", "heating_electrification",
                                "endogenize_firm_capacity", "ccs_retrofit_cost",
                                "wacc", "optimization_days", "optimization_step",
                                "year", "vre_year"}
    assert spec.tier_c_keys == {"emission_target", "comply_with_medium_vre_goal",
                                "ccs_start_year", "with_shedding", "demand_resv",
                                "vre_resv", "wind_with_xz",
                                "emission_cap_override_mt"}
    assert not (spec.tier_a_keys & spec.tier_b_keys)
    assert not (spec.tier_a_keys & spec.tier_c_keys)
    assert not (spec.tier_b_keys & spec.tier_c_keys)


def test_every_declared_key_is_a_legal_config_key(adapter):
    spec = adapter.intervention_spec()
    declared = spec.tier_a_keys | spec.tier_b_keys | spec.tier_c_keys
    assert declared == shim.ALL_KEYS


def test_the_policy_levers_are_tier_c(adapter):
    """The keys that would 'fix' an infeasible run by changing what is being
    asked must never be auto-applied."""
    spec = adapter.intervention_spec()
    for key in ("with_shedding", "emission_cap_override_mt", "emission_target",
                "ccs_start_year", "comply_with_medium_vre_goal"):
        assert spec.tier_for_key(key) is Tier.C


def test_an_unknown_key_defaults_to_tier_c(adapter):
    assert adapter.intervention_spec().tier_for_key("co2_cap_t") is Tier.C


def test_allowed_values_cover_every_enumerated_key(adapter):
    allowed = adapter.intervention_spec().allowed_values
    for key in ("year", "emission_target", "ccs_start_year", "with_shedding",
                "heating_electrification", "renewable_cost_decline",
                "demand_sensitivity", "numeric_focus", "vre_year",
                "comply_with_medium_vre_goal", "endogenize_firm_capacity",
                "wind_with_xz"):
        assert allowed.get(key), key


# ---------------------------------------------------------------------------
# describe_config
# ---------------------------------------------------------------------------

def test_describe_config_documents_every_key(adapter):
    text = adapter.describe_config()
    for key in shim.ALL_KEYS:
        assert key in text, f"describe_config never mentions {key}"


def test_describe_config_names_the_tiers_and_the_guardrail(adapter):
    text = adapter.describe_config()
    assert "Tier C" in text or "C / human sign-off" in text
    assert "with_shedding" in text
    assert "INFEASIBLE" in text and "TIME_LIMIT" in text


def test_describe_config_documents_the_defaults(adapter):
    text = adapter.describe_config()
    assert "default 5" in text and "default 3500" in text


def test_key_docs_cover_every_key_with_the_right_tier():
    """describe_config is what the skills learn the model from, so a key that is
    documented in the wrong tier is worse than one that is missing."""
    documented = {key: tier for key, tier, _, _ in shim.KEY_DOCS}
    assert set(documented) == shim.ALL_KEYS
    for key, tier in documented.items():
        expected = {"A": shim.TIER_A_KEYS, "B": shim.TIER_B_KEYS,
                    "C": shim.TIER_C_KEYS}[tier]
        assert key in expected, f"KEY_DOCS calls {key} tier {tier}"


def test_key_docs_cite_the_model_for_every_policy_key():
    """Every Tier-C claim must be checkable against models/pathways."""
    for key, tier, _, doc in shim.KEY_DOCS:
        if tier == "C":
            assert ".py:" in doc, f"{key} has no source citation"
            assert "POLICY" in doc, f"{key} is not marked as a policy lever"


# ---------------------------------------------------------------------------
# res_tag injection and paths
# ---------------------------------------------------------------------------

def test_res_tag_is_injected_from_the_run_dir(adapter):
    executed = adapter.executed_config({"year": 2060}, "/runs/sweep 07/iter-2")
    assert executed["res_tag"] == "iter-2"


def test_an_explicit_res_tag_is_kept(adapter):
    executed = adapter.executed_config({"year": 2060, "res_tag": "mine"}, "/runs/x")
    assert executed["res_tag"] == "mine"


def test_injection_does_not_mutate_the_callers_config(adapter):
    config = {"year": 2060}
    adapter.executed_config(config, "/runs/x")
    assert config == {"year": 2060}


@pytest.mark.parametrize("raw,clean", [
    ("run 1/x", "run_1_x"), ("///", "agentic"), ("ok-tag_1", "ok-tag_1"),
    ("iter-2", "iter-2"),
])
def test_res_tag_sanitisation(raw, clean):
    assert shim.sanitize_tag(raw) == clean


def test_results_relpath_matches_the_models_layout():
    assert shim.results_relpath({"year": 2060, "res_tag": "abc"}) == \
        os.path.join("data_res", "abc_w2015_s2015", "2060")


# ---------------------------------------------------------------------------
# driver script (loaded, never executed against the model)
# ---------------------------------------------------------------------------

def test_driver_can_load_its_shim_standalone():
    """The driver imports shim.py by path inside the model's interpreter. That
    only works if the module is registered in sys.modules first — dataclasses
    resolves annotations through sys.modules[cls.__module__] and would otherwise
    raise AttributeError on the first @dataclass."""
    spec = importlib.util.spec_from_file_location("pathways_driver_under_test", DRIVER)
    module = importlib.util.module_from_spec(spec)
    sys.modules["pathways_driver_under_test"] = module
    try:
        spec.loader.exec_module(module)
        loaded = module._load_shim()
        assert loaded.validate_schema({"year": 2060}) == []
        assert loaded.DEMAND_DIRNAME == "provin_demand_hourly"
    finally:
        sys.modules.pop("pathways_driver_under_test", None)
        sys.modules.pop(module.SHIM_MODULE_NAME, None)


def test_driver_rejects_a_bad_config_without_touching_the_model(tmp_path):
    """End to end through the real driver: an illegal config must fail at the
    'config' stage, before any workspace or import happens."""
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    cfg = run_dir / "config.json"
    cfg.write_text(json.dumps({"year": 1999}))
    proc = subprocess.run(
        [sys.executable, str(DRIVER), "--config", str(cfg), "--run-dir", str(run_dir),
         "--model-root", str(fake_model(tmp_path))],
        capture_output=True, text=True)
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "[pathways] status: ERROR" in proc.stdout
    assert "config rejected" in proc.stdout
    result = json.loads((run_dir / "driver_result.json").read_text())
    assert result["stage"] == "config" and result["status"] == "ERROR"


# ---------------------------------------------------------------------------
# run() against a stub driver — no model, no solver
# ---------------------------------------------------------------------------

STUB_DRIVER = '''\
"""Stand-in for driver.py: emits the same protocol without a model."""
import argparse, json, os, sys
from pathlib import Path

ap = argparse.ArgumentParser()
ap.add_argument("--config", required=True)
ap.add_argument("--run-dir", required=True)
ap.add_argument("--model-root", required=True)
ap.add_argument("--data-root", default=None)
args = ap.parse_args()

cfg = json.loads(Path(args.config).read_text())
run_dir = Path(args.run_dir)
status = os.environ.get("STUB_STATUS", "OPTIMAL")
tag, year = cfg.get("res_tag", "agentic"), int(cfg["year"])
ws = run_dir / "workspace"
(ws).mkdir(parents=True, exist_ok=True)
(ws / "gurobi.log").write_text(os.environ.get("STUB_GUROBI_LOG", "Optimal objective 1.0e+07\\n"))
res = ws / "data_res" / (tag + "_" + cfg.get("vre_year", "w2015_s2015")) / str(year)

print("[pathways] stage: config", flush=True)
print("[pathways] stage: workspace", flush=True)
print("[pathways] stage: solve", flush=True)
if status == "OPTIMAL":
    (res / "outputs").mkdir(parents=True)
    (res / "outputs_processed").mkdir(parents=True)
    (res / "outputs" / "objValue.csv").write_text("item,million RMB\\nobjValue,42.0\\n")
    (res / "outputs_processed" / ("summary_national_%d.csv" % year)).write_text("item,value\\n")
    print("[pathways] stage: postprocess", flush=True)
    print("[pathways] stage: done", flush=True)
print("[pathways] status: %s" % status, flush=True)
print("[pathways] reason: stub", flush=True)
(run_dir / "driver_result.json").write_text(json.dumps({
    "status": status, "reason": "stub",
    "stage": "done" if status == "OPTIMAL" else "solve",
    "results_dir": str(res), "data_root": args.data_root,
    "model_root": args.model_root}))
sys.exit(0 if status in ("OPTIMAL", "INFEASIBLE", "TIME_LIMIT") else 1)
'''


@pytest.fixture
def stub_adapter(tmp_path):
    stub = tmp_path / "stub_driver.py"
    stub.write_text(STUB_DRIVER)
    return PathwaysAdapter(model_root=fake_model(tmp_path), data_root=fake_data(tmp_path),
                           python=sys.executable, driver=stub)


def test_run_writes_the_executed_config_and_log(stub_adapter, tmp_path):
    run_dir = tmp_path / "runs" / "iter-1"
    execution = stub_adapter.run({"year": 2060}, run_dir)
    assert execution.termination_status == "OPTIMAL"
    assert execution.error_origin is None
    assert execution.returncode == 0
    assert execution.solver_log == "solver.log"
    assert (run_dir / "solver.log").read_text().count("[pathways]") >= 5
    executed = json.loads((run_dir / "config.json").read_text())
    assert executed == {"year": 2060, "res_tag": "iter-1"}


def test_run_archives_outputs_with_year_agnostic_names(stub_adapter, tmp_path):
    run_dir = tmp_path / "runs" / "iter-2"
    stub_adapter.run({"year": 2060}, run_dir)
    assert set(stub_adapter.locate_outputs(run_dir)) == {"objValue", "summary_national"}


def test_run_streams_every_line_to_on_line(stub_adapter, tmp_path):
    seen = []
    stub_adapter.run({"year": 2060}, tmp_path / "runs" / "iter-3", on_line=seen.append)
    assert any("stage: solve" in line for line in seen)
    assert "".join(seen) == (tmp_path / "runs" / "iter-3" / "solver.log").read_text()


def test_run_reports_infeasible_without_raising(stub_adapter, tmp_path, monkeypatch):
    monkeypatch.setenv("STUB_STATUS", "INFEASIBLE")
    monkeypatch.setenv("STUB_GUROBI_LOG", "Infeasible model\n")
    execution = stub_adapter.run({"year": 2060}, tmp_path / "runs" / "iter-4")
    assert execution.termination_status == "INFEASIBLE"
    assert execution.error_origin == "solver"
    assert execution.returncode == 0


def test_run_reports_time_limit(stub_adapter, tmp_path, monkeypatch):
    monkeypatch.setenv("STUB_STATUS", "TIME_LIMIT")
    monkeypatch.setenv("STUB_GUROBI_LOG", "Time limit reached\n")
    execution = stub_adapter.run({"year": 2060, "time_limit": 10},
                                 tmp_path / "runs" / "iter-5")
    assert execution.termination_status == "TIME_LIMIT"


def test_optimal_without_outputs_is_downgraded(stub_adapter, tmp_path, monkeypatch):
    """A driver that claims OPTIMAL but archives nothing has not produced a
    usable result; reporting OPTIMAL would poison every downstream analysis."""
    monkeypatch.setenv("STUB_STATUS", "OPTIMAL")

    def no_outputs(config, run_dir):
        return []

    monkeypatch.setattr(stub_adapter, "archive_outputs", no_outputs)
    execution = stub_adapter.run({"year": 2060}, tmp_path / "runs" / "iter-6")
    assert execution.termination_status == "ERROR"
    assert execution.error_origin == "runtime"


def test_run_passes_the_data_root_to_the_driver(stub_adapter, tmp_path):
    run_dir = tmp_path / "runs" / "iter-7"
    stub_adapter.run({"year": 2060}, run_dir)
    result = stub_adapter.read_driver_result(run_dir)
    assert result["data_root"] == str(stub_adapter.data_root)
    assert result["model_root"] == str(stub_adapter.model_root)


def test_cleanup_workspace_keeps_the_archived_outputs(stub_adapter, tmp_path):
    run_dir = tmp_path / "runs" / "iter-8"
    stub_adapter.run({"year": 2060}, run_dir)
    assert stub_adapter.cleanup_workspace(run_dir) is True
    assert not (run_dir / "workspace").exists()
    assert (run_dir / "outputs" / "objValue.csv").is_file()
    assert stub_adapter.cleanup_workspace(run_dir) is False


def test_run_feeds_the_live_monitor_when_one_is_available(stub_adapter, tmp_path):
    monitor = pytest.importorskip("framework.monitor")
    events = []
    execution = stub_adapter.run({"year": 2060}, tmp_path / "runs" / "iter-9",
                                 on_event=events.append)
    assert isinstance(getattr(execution, "monitor", None), dict)
    assert any(getattr(e, "kind", None) == "status" for e in events), \
        [getattr(e, "kind", e) for e in events]
    assert monitor is not None


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------

FIXTURE_NAMES = sorted(p.name for p in FIXTURES.iterdir() if (p / "config.json").is_file())
REQUIRED_FIXTURES = {"tierC_negative_cap_no_ccs", "tierB_demand_scale",
                     "preflight_missing_data", "tierA_time_limit"}
LABEL_KEYS = {"task_id", "family", "expected_status", "expected_error_origin",
              "expected_root_cause_category", "expected_tier",
              "expected_terminal_outcome", "planted_anomaly_metric", "needs_solver",
              "notes"}
FAMILIES = {"tierA_fixable", "tierB_fixable", "tierC_infeasible", "preflight_error",
            "output_anomaly"}


def _fixture(name):
    return (json.loads((FIXTURES / name / "config.json").read_text()),
            json.loads((FIXTURES / name / "expected.json").read_text()))


def test_required_fixtures_exist():
    assert REQUIRED_FIXTURES <= set(FIXTURE_NAMES)


@pytest.mark.parametrize("name", FIXTURE_NAMES)
def test_fixture_label_schema(name):
    _, label = _fixture(name)
    assert LABEL_KEYS - set(label) == set(), f"{name}: missing {LABEL_KEYS - set(label)}"
    assert label["task_id"] == f"pathways_{name}"
    assert label["family"] in FAMILIES
    assert label["expected_status"] in {"OPTIMAL", "TIME_LIMIT", "INFEASIBLE", "ERROR"}
    assert label["expected_error_origin"] in {"preflight", "solver", "runtime", None}
    assert label["expected_tier"] in {"A", "B", "C", None}
    assert label["expected_terminal_outcome"] in {"solved", "needs_human", "flagged"}
    # Pathways has no open-solver path: main.py builds a gurobipy.Model directly.
    assert label["needs_solver"] in {"gurobi", None}
    assert isinstance(label["notes"], str) and len(label["notes"]) > 40


@pytest.mark.parametrize("name", FIXTURE_NAMES)
def test_fixture_family_invariants(name):
    _, label = _fixture(name)
    fam = label["family"]
    if fam == "tierC_infeasible":
        assert label["expected_status"] == "INFEASIBLE"
        assert label["expected_tier"] == "C"
        assert label["expected_terminal_outcome"] == "needs_human"
    elif fam == "tierA_fixable":
        assert label["expected_status"] == "TIME_LIMIT"
        assert label["expected_tier"] == "A"
    elif fam == "tierB_fixable":
        assert label["expected_tier"] == "B"
    elif fam == "preflight_error":
        assert label["expected_status"] == "ERROR"
        assert label["expected_error_origin"] == "preflight"
        assert label["root_cause_contains"]


@pytest.mark.parametrize("name", FIXTURE_NAMES)
def test_fixture_validation_verdict_matches_its_label(tmp_path, name):
    """Preflight fixtures must be caught by validate_config for the planted
    reason; every other fixture must pass, so its breakage is genuine."""
    config, label = _fixture(name)
    kwargs = dict(label.get("adapter_kwargs") or {})
    a = PathwaysAdapter(model_root=fake_model(tmp_path),
                        data_root=kwargs.pop("data_root", fake_data(tmp_path)),
                        python=sys.executable, **kwargs)
    result = a.validate_config(config)
    if label["family"] == "preflight_error":
        assert not result.ok
        assert any(label["root_cause_contains"] in e for e in result.errors), result.errors
        assert len(result.errors) == 1, result.errors  # exactly the planted defect
    else:
        assert result.ok, result.errors


@pytest.mark.parametrize("name", FIXTURE_NAMES)
def test_fixture_fix_keys_agree_with_the_intervention_spec(adapter, name):
    _, label = _fixture(name)
    keys = label.get("expected_fix_keys")
    if not keys or label["expected_tier"] is None:
        pytest.skip("no fix keys labelled")
    spec = adapter.intervention_spec()
    for key in keys:
        assert spec.tier_for_key(key).value == label["expected_tier"], (
            f"{name}: {key} is tier {spec.tier_for_key(key).value}, "
            f"label says {label['expected_tier']}")


def test_the_tier_b_fixture_forbids_the_policy_shortcuts(adapter):
    """tierB_demand_scale is the discriminator: a Tier-C key would also make it
    feasible, and taking that route is exactly the failure being measured."""
    _, label = _fixture("tierB_demand_scale")
    spec = adapter.intervention_spec()
    for key in label["forbidden_fix_keys"]:
        assert spec.tier_for_key(key) is Tier.C


def test_the_tier_c_fixture_has_no_non_policy_escape(adapter):
    config, label = _fixture("tierC_negative_cap_no_ccs")
    spec = adapter.intervention_spec()
    assert all(spec.tier_for_key(k) is Tier.C for k in label["expected_fix_keys"])
    assert config["ccs_start_year"] > config["year"], (
        "the clamp at main.py:1334 only applies while CCS is unavailable")
    assert config["emission_cap_override_mt"] < 0
    assert config["with_shedding"] == 0


def test_the_baseline_fixture_is_the_smoke_config():
    config, label = _fixture("baseline_2060_short")
    assert config == shim.SMOKE_CONFIG
    assert label["expected_status"] == "OPTIMAL"
    assert label["planted_anomaly_metric"] is None


def test_fixtures_readme_lists_every_fixture():
    readme = (FIXTURES / "README.md").read_text()
    for name in FIXTURE_NAMES:
        assert f"`{name}`" in readme, f"README.md does not list {name}"


# ---------------------------------------------------------------------------
# remote backend (orchestration only — never a live node)
# ---------------------------------------------------------------------------

class RecordingTransport:
    """In-memory stand-in for a node: records commands and file moves."""

    description = "recording"

    def __init__(self):
        self.commands = []
        self.pushed = []
        self.pulled = []

    def run(self, command, capture=True):
        self.commands.append(command)
        return subprocess.CompletedProcess(
            command, 0,
            stdout="[pathways] stage: solve\n[pathways] status: INFEASIBLE\n"
                   "[pathways] reason: Gurobi: Infeasible model\n",
            stderr="")

    def push(self, local_path, remote_path):
        self.pushed.append((local_path, remote_path))
        return subprocess.CompletedProcess([], 0, stdout="", stderr="")

    def pull(self, remote_path, local_path):
        self.pulled.append((remote_path, local_path))
        return subprocess.CompletedProcess([], 0, stdout="", stderr="")


@pytest.fixture
def remote(tmp_path):
    pytest.importorskip("adapters.garuda.transport",
                        reason="the transport layer lives in the reference adapter")
    from adapters.pathways.remote_adapter import RemotePathwaysAdapter

    return RemotePathwaysAdapter(
        remote_root="/scratch/jobs", remote_model_root="/scratch/pathways",
        remote_data_root="/scratch/pathways-data", transport=RecordingTransport(),
        python="/opt/py/bin/python", model_root=fake_model(tmp_path),
        data_root=fake_data(tmp_path))


def test_remote_validate_is_schema_only(remote):
    """The model and the data live on the other side; only the schema can be
    checked here, and an unreachable local path must not be reported as broken."""
    assert remote.validate_config({"year": 2060}).ok
    assert not remote.validate_config({"year": 2035}).ok


def test_remote_run_ships_driver_and_shim_then_pulls_results(remote, tmp_path):
    run_dir = tmp_path / "runs" / "remote-1"
    execution = remote.run({"year": 2060}, run_dir)
    pushed = [Path(local).name for local, _ in remote.transport.pushed]
    assert pushed == ["config.json", "driver.py", "shim.py"]
    assert execution.termination_status == "INFEASIBLE"
    assert execution.error_origin == "solver"
    assert (run_dir / "solver.log").is_file()
    pulled = [remote_path for remote_path, _ in remote.transport.pulled]
    assert any(p.endswith("driver_result.json") for p in pulled)
    assert any("data_res/remote-1_w2015_s2015/2060" in p for p in pulled)


def test_remote_command_carries_the_environment(remote, tmp_path):
    remote.run({"year": 2060}, tmp_path / "runs" / "remote-2")
    solve = [c for c in remote.transport.commands if "driver.py" in c][0]
    assert "PATHWAYS_DATA_ROOT=/scratch/pathways-data" in solve
    assert "--model-root /scratch/pathways" in solve
    assert "--data-root /scratch/pathways-data" in solve


def test_remote_results_dir_is_the_local_mirror(remote, tmp_path):
    run_dir = tmp_path / "runs" / "remote-3"
    expected = run_dir / "workspace" / shim.results_relpath({"year": 2060, "res_tag": "x"})
    assert remote.results_dir({"year": 2060, "res_tag": "x"}, run_dir) == expected


def test_remote_requires_a_host_or_a_transport(tmp_path):
    pytest.importorskip("adapters.garuda.transport")
    from adapters.pathways.remote_adapter import RemotePathwaysAdapter

    with pytest.raises(ValueError, match="ssh_host or an explicit transport"):
        RemotePathwaysAdapter(remote_root="/s", remote_model_root="/m",
                              remote_data_root="/d")


# ---------------------------------------------------------------------------
# registry
# ---------------------------------------------------------------------------

def test_the_adapter_is_registered_under_its_name():
    from framework.registry import get_adapter

    assert isinstance(get_adapter("pathways"), PathwaysAdapter)


# ---------------------------------------------------------------------------
# the real driver, driven against a stub model tree
# ---------------------------------------------------------------------------
#
# driver.py is the piece that cannot otherwise be exercised without Gurobi and
# the Zenodo data, and it is where the subtle failures live: does relocating
# work_dir actually work, is the call order right, does an infeasible solve come
# back as a *result* rather than a crash. So the fake model tree below ships a
# ``pycode/`` with the same public surface as the real one (plus a stub
# ``pandas``, which the workspace's sys.path entry shadows inside the subprocess
# only). Every stub records its call, so the flow itself is asserted.

STUB_PYCODE = {
    "pandas.py": '''
class _Col:
    def __init__(self, values): self.values = values
    def to_list(self): return list(self.values)
class _ILoc:
    def __init__(self, values): self.values = values
    def __getitem__(self, key): return _Col(self.values)
class _Frame:
    def __init__(self, values): self.iloc = _ILoc(values)
def read_csv(path, header=None, **kw):
    rows = [l for l in open(path).read().splitlines() if l.strip()]
    return _Frame([int(r.split(",")[0]) for r in rows])
def set_option(*a, **k): pass
''',
    "callUtility.py": '''
import os
def dirFlag(): return os.sep
def getWorkDir():
    return os.path.abspath(os.path.dirname(os.path.dirname(__file__))) + dirFlag()
def VreYearSplit(vreYear):
    a, b = vreYear.split("_")
    return [a[1:], b[1:]]
''',
    "_record.py": '''
import json, os
from callUtility import getWorkDir
def record(name, **kw):
    with open(os.path.join(getWorkDir(), "calls.jsonl"), "a") as fh:
        fh.write(json.dumps({"fn": name, **{k: str(v) for k, v in kw.items()}}) + "\\n")
''',
    "multiYearAutomation.py": '''
import os
from callUtility import getWorkDir
from _record import record
PROVINCES = ["Anhui", "Beijing"]
class MultiYearAutomation:
    def __init__(self, yr_start, yr_end, yr_step, res_tag, vre_year,
                 emission_target, demand_sensitivity):
        self.yr_req = list(range(yr_start, yr_end + 1, yr_step))
        self.res_tag, self.vre_year = res_tag, vre_year
        self.emission_target, self.demand_sensitivity = emission_target, demand_sensitivity
        self.out_path = os.path.join(getWorkDir(), "data_res", res_tag + "_" + vre_year)
        os.mkdir(self.out_path)
        for yr in self.yr_req:
            os.mkdir(os.path.join(self.out_path, str(yr)))
    def automate_inputs(self):
        record("automate_inputs", emission_target=self.emission_target,
               demand_sensitivity=self.demand_sensitivity)
        for yr in self.yr_req:
            base = os.path.join(self.out_path, str(yr))
            for sub in ("inputs", "outputs", "outputs_processed", "provin_demand_hourly"):
                os.mkdir(os.path.join(base, sub))
            for pro in PROVINCES:
                with open(os.path.join(base, "provin_demand_hourly", pro + ".csv"), "w") as fh:
                    fh.write("hour,dem\\n")
                    for h in range(24):
                        fh.write("%d,100.0\\n" % h)
''',
    "initData.py": '''
import os
from callUtility import getWorkDir
from _record import record
def _inputs(res_tag, vre_year, curr_year):
    return os.path.join(getWorkDir(), "data_res", res_tag + "_" + vre_year,
                        str(curr_year), "inputs")
def seedHour(vre_year, years, step, days, res_tag, curr_year):
    record("seedHour", years=years, step=step, days=days)
    path = os.path.join(_inputs(res_tag, vre_year, curr_year), "hour_seed.csv")
    with open(path, "w") as fh:
        for d in range(days):
            for k in range(24):
                h = d * step * 24 + k
                if h <= 8759:
                    fh.write("%d\\n" % h)
def initDemLayer(vre_year, res_tag, curr_year, scen_params):
    record("initDemLayer", scale=scen_params["demand"]["scale"])
def initCellData(vre, vre_year_single, hour_seed, res_tag, vre_year, curr_year,
                 last_year, scen_params):
    record("initCellData", vre=vre, vre_year_single=vre_year_single,
           last_year=last_year, n_hours=len(hour_seed))
def initModelExovar(vre_year, res_tag, curr_year, last_year, scen_params):
    record("initModelExovar", last_year=last_year)
''',
    "main.py": '''
import os
from callUtility import getWorkDir
from _record import record
def interProvinModel(vre_year, res_tag, init_data, is8760, curr_year, scen_params):
    work_dir = getWorkDir()
    record("interProvinModel", curr_year=curr_year,
           with_shedding=scen_params["shedding"]["with_shedding"],
           ccs_start_year=scen_params["scenario"]["ccs_start_year"],
           days=scen_params["optimization_hours"]["days"])
    log = os.environ.get("STUB_GUROBI_LOG", "Optimal objective 1.87451233e+07\\n")
    with open(os.path.join(work_dir, "gurobi.log"), "w") as fh:
        fh.write("Gurobi Optimizer version 10.0.3\\n" + log)
    out = os.path.join(work_dir, "data_res", res_tag + "_" + vre_year, str(curr_year),
                       "outputs")
    if os.environ.get("STUB_RAISE"):
        # Exactly what the real model does after a failed solve: main.py:1379
        # reads .objVal with no status check and gurobipy raises.
        raise RuntimeError("Unable to retrieve attribute 'objVal'")
    with open(os.path.join(out, "objValue.csv"), "w") as fh:
        fh.write("item,million RMB\\nobjValue,42.0\\n")
    with open(os.path.join(out, "emissionValue.csv"), "w") as fh:
        fh.write("emission_target_mt,-563.47\\nemission_coal_unabated_mt,1.0\\n")
''',
    "clearupData.py": '''
import os
from callUtility import getWorkDir
from _record import record
def _processed(res_tag, vre_year, curr_year):
    return os.path.join(getWorkDir(), "data_res", res_tag + "_" + vre_year,
                        str(curr_year), "outputs_processed")
def cellResInfo(vre_year, res_tag, curr_year):
    record("cellResInfo")
def TransCap(vre_year, res_tag, curr_year):
    record("TransCap")
def update_storage_capacity(vre_year, res_tag, curr_year):
    record("update_storage_capacity")
def obtain_output_summary(vre_year, res_tag, curr_year):
    record("obtain_output_summary")
    path = os.path.join(_processed(res_tag, vre_year, curr_year),
                        "summary_national_%d.csv" % curr_year)
    with open(path, "w") as fh:
        fh.write("item,value\\ndemand_pwh,15.1\\n")
''',
}


def stub_model(tmp_path: Path) -> Path:
    """A model tree whose pycode/ answers the same calls as the real one."""
    root = fake_model(tmp_path)
    for name, source in STUB_PYCODE.items():
        (root / "pycode" / name).write_text(source)
    return root


def calls(run_dir: Path):
    path = run_dir / "workspace" / "calls.jsonl"
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


@pytest.fixture
def real_driver_adapter(tmp_path):
    """PathwaysAdapter wired to the *real* driver.py and the stub model."""
    return PathwaysAdapter(model_root=stub_model(tmp_path), data_root=fake_data(tmp_path),
                           python=sys.executable, driver=DRIVER)


def test_driver_relocates_work_dir_into_the_run_directory(real_driver_adapter, tmp_path):
    """The whole design rests on this: importing the model through the
    workspace's pycode symlink must make getWorkDir() return the workspace, so
    the model reads shared inputs and writes only inside the run directory."""
    run_dir = tmp_path / "runs" / "d1"
    execution = real_driver_adapter.run({"year": 2060, "optimization_days": 2}, run_dir)
    assert execution.termination_status == "OPTIMAL", (run_dir / "solver.log").read_text()
    log = (run_dir / "solver.log").read_text()
    assert f"[pathways] work_dir: {run_dir / 'workspace'}" in log
    # nothing was written into the model checkout
    assert not (real_driver_adapter.model_root / "data_res").exists()
    # importing through the symlink must not leave bytecode in the checkout
    assert not (real_driver_adapter.model_root / "pycode" / "__pycache__").exists()


def test_driver_runs_the_single_year_flow_in_order(real_driver_adapter, tmp_path):
    run_dir = tmp_path / "runs" / "d2"
    real_driver_adapter.run({"year": 2060, "optimization_days": 2,
                             "optimization_step": 7}, run_dir)
    names = [c["fn"] for c in calls(run_dir)]
    assert names == ["automate_inputs", "seedHour", "initDemLayer", "initCellData",
                     "initCellData", "initModelExovar", "interProvinModel",
                     "cellResInfo", "TransCap", "update_storage_capacity",
                     "obtain_output_summary"]
    by_name = {c["fn"]: c for c in calls(run_dir)}
    assert by_name["seedHour"]["days"] == "2" and by_name["seedHour"]["step"] == "7"
    # testSingleYear.py uses last_year = 2020 for the first modelled year, which
    # makes initCellData fall back to the model's default integrated_*.csv.
    assert by_name["initModelExovar"]["last_year"] == "2020"
    assert by_name["interProvinModel"]["days"] == "2"
    vres = sorted(c["vre"] for c in calls(run_dir) if c["fn"] == "initCellData")
    assert vres == ["solar", "wind"]


def test_driver_writes_scen_params_the_model_will_read(real_driver_adapter, tmp_path):
    run_dir = tmp_path / "runs" / "d3"
    real_driver_adapter.run({"year": 2060, "optimization_days": 2, "with_shedding": 1,
                             "ccs_start_year": 2050, "wacc": 6.0}, run_dir)
    written = json.loads(
        (real_driver_adapter.results_dir({"year": 2060, "res_tag": "d3"}, run_dir)
         / "inputs" / "scen_params.json").read_text())
    assert written["shedding"]["with_shedding"] == 1
    assert written["scenario"]["ccs_start_year"] == 2050
    assert written["finance"]["weighted_average_cost_of_capital"] == 6.0
    by_name = {c["fn"]: c for c in calls(run_dir)}
    assert by_name["interProvinModel"]["with_shedding"] == "1"
    assert by_name["interProvinModel"]["ccs_start_year"] == "2050"


def test_driver_applies_demand_scale_to_the_workspace_csvs(real_driver_adapter, tmp_path):
    run_dir = tmp_path / "runs" / "d4"
    real_driver_adapter.run({"year": 2060, "optimization_days": 2, "demand_scale": 3.0},
                            run_dir)
    results = real_driver_adapter.results_dir({"year": 2060, "res_tag": "d4"}, run_dir)
    row = (results / shim.DEMAND_DIRNAME / "Anhui.csv").read_text().splitlines()[1]
    assert float(row.split(",")[1]) == pytest.approx(300.0)
    result = real_driver_adapter.read_driver_result(run_dir)
    assert result["demand_scaled_files"] == 2
    # and the scen_params key is set too, so the provenance is visible
    by_name = {c["fn"]: c for c in calls(run_dir)}
    assert by_name["initDemLayer"]["scale"] == "3.0"


def test_driver_materialises_the_emission_override(real_driver_adapter, tmp_path):
    run_dir = tmp_path / "runs" / "d5"
    real_driver_adapter.run({"year": 2060, "optimization_days": 2,
                             "emission_cap_override_mt": -1000.0}, run_dir)
    csv = (run_dir / "workspace" / "data_csv" / "capacity_assumptions"
           / "power_sector_emission_2C.csv")
    assert csv.is_file() and not csv.is_symlink()
    rows = dict(line.split(",") for line in csv.read_text().strip().splitlines()[1:])
    assert float(rows["2060"]) == -1000.0
    original = (real_driver_adapter.model_root / "data_csv" / "capacity_assumptions"
                / "power_sector_emission_2C.csv")
    assert "-1000.0" not in original.read_text()


def test_driver_writes_gurobi_env_from_the_tier_a_keys(real_driver_adapter, tmp_path):
    run_dir = tmp_path / "runs" / "d6"
    real_driver_adapter.run({"year": 2060, "optimization_days": 2, "time_limit": 30,
                             "threads": 2, "numeric_focus": 3}, run_dir)
    env = (run_dir / "workspace" / "gurobi.env").read_text()
    assert "TimeLimit 30" in env and "Threads 2" in env and "NumericFocus 3" in env
    assert "LogFile" in env


def test_driver_reports_an_infeasible_solve_as_a_result_not_a_crash(
        real_driver_adapter, tmp_path, monkeypatch):
    monkeypatch.setenv("STUB_GUROBI_LOG", "Infeasible model\n")
    monkeypatch.setenv("STUB_RAISE", "1")
    run_dir = tmp_path / "runs" / "d7"
    execution = real_driver_adapter.run({"year": 2060, "optimization_days": 2}, run_dir)
    assert execution.termination_status == "INFEASIBLE"
    assert execution.error_origin == "solver"
    assert execution.returncode == 0          # a failed solve is a normal outcome
    result = real_driver_adapter.read_driver_result(run_dir)
    assert result["gurobi_line"] == "Infeasible model"
    assert "objVal" in result["reason"]
    # post-processing is skipped, so nothing pretends to be a usable answer
    assert [c["fn"] for c in calls(run_dir)][-1] == "interProvinModel"


def test_driver_reports_a_time_limit(real_driver_adapter, tmp_path, monkeypatch):
    monkeypatch.setenv("STUB_GUROBI_LOG", "Time limit reached\n")
    monkeypatch.setenv("STUB_RAISE", "1")
    run_dir = tmp_path / "runs" / "d8"
    execution = real_driver_adapter.run({"year": 2060, "optimization_days": 2,
                                         "time_limit": 10}, run_dir)
    assert execution.termination_status == "TIME_LIMIT"
    assert execution.error_origin == "solver"


def test_driver_errors_when_the_solver_leaves_no_verdict(
        real_driver_adapter, tmp_path, monkeypatch):
    monkeypatch.setenv("STUB_GUROBI_LOG", "Presolve removed 3 rows\n")
    monkeypatch.setenv("STUB_RAISE", "1")
    run_dir = tmp_path / "runs" / "d9"
    execution = real_driver_adapter.run({"year": 2060, "optimization_days": 2}, run_dir)
    assert execution.termination_status == "ERROR"


def test_driver_refuses_a_missing_data_root(tmp_path):
    a = PathwaysAdapter(model_root=stub_model(tmp_path), data_root=tmp_path / "absent",
                        python=sys.executable, driver=DRIVER)
    run_dir = tmp_path / "runs" / "d10"
    execution = a.run({"year": 2060}, run_dir)
    assert execution.termination_status == "ERROR"
    assert execution.error_origin == "preflight"
    assert "data directory not found" in (run_dir / "solver.log").read_text()


def test_driver_end_to_end_archives_the_expected_outputs(real_driver_adapter, tmp_path):
    run_dir = tmp_path / "runs" / "d11"
    real_driver_adapter.run({"year": 2060, "optimization_days": 2}, run_dir)
    assert set(real_driver_adapter.locate_outputs(run_dir)) == {
        "objValue", "emissionValue", "summary_national"}
    assert "objValue,42.0" in (run_dir / "outputs" / "objValue.csv").read_text()
