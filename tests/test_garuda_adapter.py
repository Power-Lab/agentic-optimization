"""GarudaAdapter unit tests — no Julia, no solver.

The model is replaced by a stub ``julia`` executable (a shell wrapper around a
small Python script) that speaks the model's protocol: reads ``--config``, honours
``--preflight-only``, prints the engine's status lines, writes result CSVs into
``<model_root>/results/<name>__<run_tag>/`` and exits non-zero after an
infeasible solve exactly like the real model does. That exercises the whole
``run()`` path — config marshalling, run_tag injection, line-by-line log
streaming, status parsing, output archiving — deterministically in milliseconds.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
import time
from pathlib import Path

import pytest

from adapters.garuda import (
    GarudaAdapter,
    REGRESSION_CONFIG,
    REGRESSION_HEADLINES,
    RemoteGarudaAdapter,
    check_regression_headlines,
    describe_schema,
    read_headline,
    sanitise_run_tag,
    stream_local,
)
from adapters.garuda.garuda_adapter import (
    KNOWN_KEYS,
    OPTIONAL_KEYS,
    REQUIRED_KEYS,
    RESULT_CSVS,
    TIER_A_KEYS,
    TIER_B_KEYS,
    TIER_C_KEYS,
)
from adapters.garuda.transport import Transport
from framework import get_adapter, run_and_record

ROOT = Path(__file__).resolve().parents[1]

BASE_CFG = dict(REGRESSION_CONFIG)


# --------------------------------------------------------------------------
# stub model
# --------------------------------------------------------------------------

FAKE_MODEL = r'''
import json, os, sys
from pathlib import Path

# argv: --project=<root> run_model.jl --config <cfg> [--preflight-only]
args = sys.argv[1:]
root = next(a for a in args if a.startswith("--project=")).split("=", 1)[1]
assert "run_model.jl" in args, args
cfg_path = args[args.index("--config") + 1]
cfg = json.load(open(cfg_path))
assert os.path.realpath(os.getcwd()) == os.path.realpath(root), (os.getcwd(), root)
print("GARUDA_PYTHON=" + os.environ.get("GARUDA_PYTHON", "<unset>"))
print("GARUDA_SKIP_VALIDATION=" + os.environ.get("GARUDA_SKIP_VALIDATION", "<unset>"))
name = f"{cfg['scenario']}_{cfg['island']}_{cfg['year']}_{cfg['clean']}"
if cfg.get("run_tag"):
    name += "__" + cfg["run_tag"]
if cfg.get("island") == "atlantis":
    print(f"ERROR: LoadError: Input data directory not found: {root}/data_indonesia/2030/atlantis",
          file=sys.stderr)
    sys.exit(1)
if "--preflight-only" in args:
    print(f"Preflight checks passed for {name}")
    sys.exit(0)
print("Running HiGHS 1.7.0 (git hash: n/a): Copyright (c) 2024 HiGHS under MIT licence terms")
print("Presolving model")
print("3 rows, 4 cols, 6 nonzeros  0s")
sys.stdout.flush()
if cfg.get("clean") == "clean" and cfg["CO2_limit"] < 0:
    print("Model status        : Infeasible")
    print("Dispatch is infeasible (fleet cannot meet demand even with full non-served energy).")
    sys.stdout.flush()
    print("ERROR: LoadError: Result index of attribute MathOptInterface.ObjectiveValue(1) out of bounds. "
          "There are currently 0 solution(s) in the model.", file=sys.stderr)
    sys.exit(1)
if cfg.get("time_limit", 1e9) < 10:
    print("Capacity expansion reached the time limit (MILP, exact UC).")
    sys.exit(0)
out = Path(root) / "results" / name
out.mkdir(parents=True, exist_ok=True)
(out / "cost_results.csv").write_text("Total_Costs,NSE_Costs\n418.75,12.0\n")
(out / "clean_energy_results.csv").write_text("CO2_Emissions,Grid_REShare\n889909.7,0.0\n")
(out / "reliability_results.csv").write_text("Zone,Total_NSE_MWh\n1,100000.0\n2,105997.3\n")
(out / "notes.txt").write_text("not a csv\n")
print("  Gap                0.42% (tolerance: 1%)")
engine = cfg.get("engine", "expansion")
if engine == "dispatch":
    print("Dispatch solved successfully (LP, UC relaxed).")
else:
    print("Capacity expansion solved successfully (MILP, exact UC).")
'''

INPUT_FILES = ["generators.csv", "demand.csv", "generators_variability.csv", "fuels_data.csv"]
SITE_FILES = ["village_generators.csv", "village_demand.csv", "village_demandheat.csv",
              "village_generators_variability.csv", "network.csv"]


def fake_model_root(tmp_path: Path) -> Path:
    """A model tree with a 2030 maluku (grid-only) and timor_demo (site) dataset."""
    root = tmp_path / "model"
    (root / "functions").mkdir(parents=True)
    (root / "run_model.jl").write_text("# stub\n")
    for island, files in (("maluku", INPUT_FILES), ("timor_demo", INPUT_FILES + SITE_FILES)):
        d = root / "data_indonesia" / "2030" / island
        d.mkdir(parents=True)
        for f in files:
            (d / f).write_text("a,b\n1,2\n")
    return root


def fake_julia(tmp_path: Path) -> Path:
    script = tmp_path / "fake_model.py"
    script.write_text(FAKE_MODEL)
    stub = tmp_path / "julia"
    stub.write_text(f"#!/bin/sh\nexec {shlex.quote(sys.executable)} "
                    f"{shlex.quote(str(script))} \"$@\"\n")
    stub.chmod(0o755)
    return stub


@pytest.fixture
def adapter(tmp_path):
    return GarudaAdapter(model_root=fake_model_root(tmp_path), julia=str(fake_julia(tmp_path)),
                         python_for_validator="/opt/fake/python")


# --------------------------------------------------------------------------
# status parsing — every marker line the engines / preflight can print
# --------------------------------------------------------------------------

@pytest.mark.parametrize("output,rc,status,origin", [
    # engines, happy paths (optimizer.jl ~934, dispatch_engine.jl ~56)
    ("Capacity expansion solved successfully (MILP, exact UC).\n", 0, "OPTIMAL", None),
    ("Capacity expansion solved successfully (LP, UC relaxed).\n", 0, "OPTIMAL", None),
    ("Dispatch solved successfully (LP, UC relaxed).\n", 0, "OPTIMAL", None),
    ("Dispatch solved successfully (MILP, exact UC).\n", 0, "OPTIMAL", None),
    # time limit: incumbent extracted (rc 0) or nothing to extract (rc 1)
    ("Capacity expansion reached the time limit (MILP, exact UC).\n", 0, "TIME_LIMIT", None),
    ("Dispatch reached the time limit.\n", 0, "TIME_LIMIT", None),
    ("Dispatch reached the time limit.\nERROR: LoadError: Result index ... out of bounds\n",
     1, "TIME_LIMIT", "runtime"),
    # infeasible: the model then crashes on objective_value, so rc != 0 is normal
    ("Capacity expansion is infeasible.\nERROR: LoadError: Result index of attribute "
     "MathOptInterface.ObjectiveValue(1) out of bounds.\n", 1, "INFEASIBLE", "solver"),
    ("Dispatch is infeasible (fleet cannot meet demand even with full non-served energy).\n",
     1, "INFEASIBLE", "solver"),
    ("Dispatch is infeasible (fleet cannot meet demand even with full non-served energy).\n",
     0, "INFEASIBLE", "solver"),
    # did not solve
    ("Capacity expansion did not solve. Termination status: NUMERICAL_ERROR\n", 1, "ERROR", "solver"),
    ("Dispatch did not solve. Termination status: OTHER_ERROR\n", 1, "ERROR", "solver"),
    # Gurobi's default INFEASIBLE_OR_UNBOUNDED verdict is an infeasibility, not an error
    ("Dispatch did not solve. Termination status: INFEASIBLE_OR_UNBOUNDED\n", 1, "INFEASIBLE", "solver"),
    ("Capacity expansion did not solve. Termination status: DUAL_INFEASIBLE\n", 1, "INFEASIBLE", "solver"),
    # the solve finished but result extraction crashed: outputs untrustworthy
    ("Dispatch solved successfully (LP, UC relaxed).\nERROR: LoadError: BoundsError\n", 1, "ERROR", "runtime"),
    # preflight gate
    ("Preflight checks passed for base_maluku_2030_reference\n", 0, "PREFLIGHT_OK", None),
    ("ERROR: LoadError: Config file not found: /x/config.json\n", 1, "ERROR", "preflight"),
    ("ERROR: LoadError: Config file is missing required keys: CO2_limit\n", 1, "ERROR", "preflight"),
    ("ERROR: LoadError: Unknown scenario: frobnicate. Valid values: base, grid, ...\n", 1, "ERROR", "preflight"),
    ("ERROR: LoadError: Unknown clean flag: dirty\n", 1, "ERROR", "preflight"),
    ("ERROR: LoadError: Unknown solver \"cplex\"; expected \"highs\" or \"gurobi\".\n", 1, "ERROR", "preflight"),
    ("ERROR: LoadError: Input data directory not found: /m/data_indonesia/2030/atlantis\n", 1, "ERROR", "preflight"),
    ("ERROR: LoadError: Missing required input files in /m/data_indonesia/2030/maluku: site_generators.csv\n",
     1, "ERROR", "preflight"),
    ("ERROR: LoadError: Input schema validation failed:\n[demand.csv] column missing\n", 1, "ERROR", "preflight"),
    ("ERROR: LoadError: Solver \"gurobi\" could not start a working optimizer session: no licence\n",
     1, "ERROR", "preflight"),
    ("ERROR: LoadError: config key time_limit must be > 0 seconds, got 0.0\n", 1, "ERROR", "preflight"),
    ("ERROR: LoadError: config key lp_method must be in -1..5 (Gurobi Method), got 7\n", 1, "ERROR", "preflight"),
    ("ERROR: LoadError: config key policy_scope must be \"grid\" or \"system\", got \"national\"\n",
     1, "ERROR", "preflight"),
    ("ERROR: LoadError: ArgumentError: Package Gurobi not found in current path.\n", 1, "ERROR", "preflight"),
    # solver-native wording with no engine line (crash before the println)
    ("Model is infeasible\nERROR: LoadError: something\n", 1, "INFEASIBLE", "solver"),
    ("Infeasible or unbounded model\n", 1, "INFEASIBLE", "solver"),
    # nothing recognisable
    ("", 0, "UNKNOWN", None),
    ("Running HiGHS 1.7.0\n", 0, "UNKNOWN", None),
    ("ERROR: LoadError: BoundsError: attempt to access 3-element Vector\n", 1, "ERROR", "runtime"),
    ("ERROR: LoadError: something at functions/preflight.jl:193\n", 1, "ERROR", "preflight"),
    ("", None, "UNKNOWN", None),
])
def test_parse_status(output, rc, status, origin):
    assert GarudaAdapter._parse_status(output, rc) == (status, origin)


def test_engine_marker_beats_solver_chatter():
    # HiGHS/Gurobi logs mention "infeasib..." in normal chatter; the engine's
    # verdict must win.
    log = ("Presolve : Reductions: rows 10(-2); columns 12(-3)\n"
           "Dual simplex ... 0 infeasibilities\n"
           "Dispatch solved successfully (LP, UC relaxed).\n")
    assert GarudaAdapter._parse_status(log, 0) == ("OPTIMAL", None)
    log = "Model is infeasible\nCapacity expansion is infeasible.\nERROR: LoadError: x\n"
    assert GarudaAdapter._parse_status(log, 1) == ("INFEASIBLE", "solver")


def test_parse_status_never_raises_on_odd_input():
    assert GarudaAdapter._parse_status("\x00\xff weird", 137) == ("ERROR", "runtime")


@pytest.mark.parametrize("output,gap", [
    ("Best objective 1.234560e+02, best bound 1.230000e+02, gap 0.3241%\n", 0.003241),
    ("Explored 10 nodes\nBest objective 5.0e+02, best bound 4.9e+02, gap 2.0000%\n"
     "Best objective 5.0e+02, best bound 4.99e+02, gap 0.2000%\n", 0.002),   # last one wins
    ("Solving report\n  Status            Optimal\n  Gap               0.42% (tolerance: 1%)\n", 0.0042),
    ("Dispatch solved successfully (LP, UC relaxed).\n", None),
])
def test_parse_gap(output, gap):
    got = GarudaAdapter._parse_gap(output)
    if gap is None:
        assert got is None
    else:
        assert got == pytest.approx(gap)


# --------------------------------------------------------------------------
# run_tag injection
# --------------------------------------------------------------------------

@pytest.mark.parametrize("raw,tag", [
    ("iter00_0123abcd", "iter00_0123abcd"),
    ("smoke_garuda_maluku_dispatch", "smoke_garuda_maluku_dispatch"),
    ("my run (2)!", "my_run_2"),
    ("  spaces  ", "spaces"),
    ("..dots..", "dots"),
    ("", "run"),
    ("///", "run"),
    ("a/b/c", "a_b_c"),
])
def test_sanitise_run_tag(raw, tag):
    assert sanitise_run_tag(raw) == tag


def test_run_tag_injected_from_run_dir_basename():
    executed = GarudaAdapter.executed_config(BASE_CFG, "/runs/iter03_deadbeef")
    assert executed["run_tag"] == "iter03_deadbeef"
    assert "run_tag" not in BASE_CFG                       # caller's dict untouched
    assert {k: v for k, v in executed.items() if k != "run_tag"} == BASE_CFG


def test_run_tag_respected_when_present():
    cfg = dict(BASE_CFG, run_tag="sweep_a")
    assert GarudaAdapter.executed_config(cfg, "/runs/iter00_x")["run_tag"] == "sweep_a"
    # blank counts as absent
    cfg = dict(BASE_CFG, run_tag="   ")
    assert GarudaAdapter.executed_config(cfg, "/runs/iter00_x")["run_tag"] == "iter00_x"


def test_results_name_follows_preflight_rule():
    assert GarudaAdapter.results_name(BASE_CFG) == "base_maluku_2030_reference"
    assert GarudaAdapter.results_name(dict(BASE_CFG, run_tag="t1")) == "base_maluku_2030_reference__t1"
    assert GarudaAdapter.results_name(dict(BASE_CFG, run_tag=" ")) == "base_maluku_2030_reference"


# --------------------------------------------------------------------------
# validation
# --------------------------------------------------------------------------

def test_schema_declares_every_optional_key_once():
    optional = [k.name for k in OPTIONAL_KEYS if k.name not in REQUIRED_KEYS]
    assert sorted(optional) == sorted([
        "mipgap", "RE_limit", "village_storage_max_mwh", "battery_duration_h", "engine",
        "relax_uc", "exact_connect", "lp_method", "time_limit", "import_price",
        "export_price", "policy_scope", "export_backed_by_generation", "solver", "run_tag",
    ])
    assert len(optional) == 15
    assert KNOWN_KEYS == set(REQUIRED_KEYS) | set(optional)
    assert TIER_A_KEYS | TIER_B_KEYS | TIER_C_KEYS == KNOWN_KEYS - {"island", "year"}


@pytest.mark.parametrize("override,needle", [
    ({"scenario": "frobnicate"}, "Unknown scenario"),
    ({"clean": "dirty"}, "Unknown clean"),
    ({"engine": "simulation"}, "Unknown engine"),
    ({"solver": "cplex"}, "Unknown solver"),
    ({"policy_scope": "national"}, "Unknown policy_scope"),
    ({"lp_method": 7}, "Unknown lp_method"),
    ({"lp_method": 2.5}, "lp_method must be an integer"),
    ({"time_limit": 0}, "time_limit must be > 0"),
    ({"time_limit": -5}, "time_limit must be > 0"),
    ({"battery_duration_h": -1}, "battery_duration_h must be >= 0"),
    ({"mipgap": -0.1}, "mipgap must be >= 0"),
    ({"mipgap": "0.01"}, "mipgap must be a number"),
    ({"mipgap": True}, "mipgap must be a number"),
    ({"relax_uc": "yes"}, "relax_uc must be a boolean"),
    ({"CO235reduction": 0}, "CO235reduction must be a boolean"),
    ({"run_tag": 3}, "run_tag must be a string"),
    ({"year": 2030}, "year must be a string"),
    ({"island": 7}, "island must be a string"),
    ({"solar_share": 0.5}, "Unknown config key 'solar_share'"),
])
def test_validate_schema_rejects(override, needle):
    errors = GarudaAdapter.validate_schema(dict(BASE_CFG, **override))
    assert any(needle in e for e in errors), errors


def test_validate_schema_missing_required_keys():
    cfg = {k: v for k, v in BASE_CFG.items() if k not in ("CO2_limit", "clean")}
    errors = GarudaAdapter.validate_schema(cfg)
    assert any("Missing required config keys" in e and "CO2_limit" in e and "clean" in e
               for e in errors), errors
    assert GarudaAdapter.validate_schema("not a dict") == ["config must be a JSON object"]


def test_validate_schema_accepts_every_optional_key_at_a_legal_value():
    cfg = dict(BASE_CFG, mipgap=0.05, RE_limit=0.34, village_storage_max_mwh=208.0,
               battery_duration_h=4, engine="expansion", relax_uc=False, exact_connect=True,
               lp_method=2, time_limit=3600, import_price=59.0, export_price=10.0,
               policy_scope="system", export_backed_by_generation=True, solver="gurobi",
               run_tag="x")
    assert GarudaAdapter.validate_schema(cfg) == []


def test_validate_config_checks_inputs_and_site_tables(adapter):
    assert adapter.validate_config(BASE_CFG).ok
    r = adapter.validate_config(dict(BASE_CFG, island="atlantis"))
    assert not r.ok and any("Input data directory not found" in e for e in r.errors)
    r = adapter.validate_config(dict(BASE_CFG, scenario="village"))      # maluku: no site layer
    assert not r.ok and any("Missing required input files" in e and "site_generators.csv" in e
                            for e in r.errors)
    r = adapter.validate_config(dict(BASE_CFG, scenario="grid"))         # maluku: no network.csv
    assert not r.ok and any("network.csv" in e for e in r.errors)
    # timor_demo ships village_* spellings, accepted for the site_* requirement
    assert adapter.validate_config(dict(BASE_CFG, island="timor_demo", scenario="gridvillage")).ok
    assert adapter.validate_config(dict(BASE_CFG, island="timor_demo", scenario="captive")).ok


def test_validate_config_on_the_real_submodule_if_present():
    real = GarudaAdapter()
    if not (real.model_root / "run_model.jl").exists():
        pytest.skip("models/garuda submodule not initialised")
    assert real.validate_config(REGRESSION_CONFIG).ok
    assert not real.validate_config(dict(REGRESSION_CONFIG, island="atlantis")).ok


# --------------------------------------------------------------------------
# outputs archiving
# --------------------------------------------------------------------------

def test_archive_outputs_copies_only_csvs_and_locate_outputs_maps_stems(adapter, tmp_path):
    cfg = dict(BASE_CFG, run_tag="t1")
    src = adapter.model_results_dir(cfg)
    assert src == adapter.model_root / "results" / "base_maluku_2030_reference__t1"
    src.mkdir(parents=True)
    for stem in RESULT_CSVS[:5]:
        (src / f"{stem}.csv").write_text(f"{stem}\n1\n")
    (src / "solver.lp").write_text("not a result\n")
    run_dir = tmp_path / "run"
    copied = adapter._archive_outputs(cfg, run_dir)
    assert sorted(p.name for p in copied) == sorted(f"{s}.csv" for s in RESULT_CSVS[:5])
    outputs = adapter.locate_outputs(run_dir)
    assert set(outputs) == set(RESULT_CSVS[:5])
    assert all(p.parent == run_dir / "outputs" for p in outputs.values())
    assert (run_dir / "outputs" / "cost_results.csv").read_text() == "cost_results\n1\n"


def test_archive_outputs_skips_stale_files_from_a_previous_run(adapter, tmp_path):
    cfg = dict(BASE_CFG, run_tag="t2")
    src = adapter.model_results_dir(cfg)
    src.mkdir(parents=True)
    stale = src / "cost_results.csv"
    stale.write_text("old\n")
    old = time.time() - 3600
    os.utime(stale, (old, old))
    fresh = src / "nse_results.csv"
    fresh.write_text("new\n")
    copied = adapter._archive_outputs(cfg, tmp_path / "run", since=time.time() - 60)
    assert [p.name for p in copied] == ["nse_results.csv"]


def test_archive_and_locate_outputs_when_nothing_exists(adapter, tmp_path):
    assert adapter._archive_outputs(dict(BASE_CFG, run_tag="none"), tmp_path / "run") == []
    assert adapter.locate_outputs(tmp_path / "run") == {}


# --------------------------------------------------------------------------
# run() end to end against the stub model
# --------------------------------------------------------------------------

def test_run_optimal_end_to_end(adapter, tmp_path):
    run_dir = tmp_path / "runs" / "iter00_abc"
    seen = []
    ex = adapter.run(BASE_CFG, run_dir, on_line=seen.append)

    assert ex.termination_status == "OPTIMAL"
    assert ex.error_origin is None
    assert ex.returncode == 0
    assert ex.solver_log == "solver.log"
    assert ex.wall_seconds is not None and ex.wall_seconds >= 0
    assert ex.mipgap_reached == pytest.approx(0.0042)

    # executed config carries the injected run_tag; the results folder used it
    executed = json.loads((run_dir / "config.json").read_text())
    assert executed["run_tag"] == "iter00_abc"
    assert (adapter.model_root / "results" / "base_maluku_2030_reference__iter00_abc").is_dir()

    # solver.log is the merged stream, and on_line saw the same lines in order
    log = (run_dir / "solver.log").read_text()
    assert "Dispatch solved successfully (LP, UC relaxed)." in log
    assert "GARUDA_PYTHON=/opt/fake/python" in log            # validator env injected
    assert "GARUDA_SKIP_VALIDATION=<unset>" in log
    assert "".join(seen) == log

    # only CSVs archived
    outputs = adapter.locate_outputs(run_dir)
    assert set(outputs) == {"cost_results", "clean_energy_results", "reliability_results"}
    assert not (run_dir / "outputs" / "notes.txt").exists()


def test_run_and_record_keeps_run_tag_out_of_record_config(adapter, tmp_path):
    run_dir = tmp_path / "iter00_hash"
    record = run_and_record(adapter, BASE_CFG, run_dir)
    assert record.config == BASE_CFG                         # no run_tag leaked in
    assert json.loads((run_dir / "config.json").read_text())["run_tag"] == "iter00_hash"
    assert record.execution.termination_status == "OPTIMAL"
    assert (run_dir / "run_record.json").exists()


def test_run_infeasible_does_not_raise(adapter, tmp_path):
    cfg = dict(BASE_CFG, clean="clean", CO2_limit=-1, RE_limit=0.0)
    ex = adapter.run(cfg, tmp_path / "iter00_inf")
    assert ex.termination_status == "INFEASIBLE"
    assert ex.error_origin == "solver"
    assert ex.returncode == 1
    assert "Dispatch is infeasible" in (tmp_path / "iter00_inf" / "solver.log").read_text()
    assert adapter.locate_outputs(tmp_path / "iter00_inf") == {}


def test_run_time_limit(adapter, tmp_path):
    cfg = dict(BASE_CFG, engine="expansion", relax_uc=False, time_limit=5)
    ex = adapter.run(cfg, tmp_path / "iter00_tl")
    assert (ex.termination_status, ex.error_origin) == ("TIME_LIMIT", None)


def test_run_model_preflight_error(adapter, tmp_path):
    # bypass the adapter's own validate_config and let the model's gate speak
    ex = adapter.run(dict(BASE_CFG, island="atlantis"), tmp_path / "iter00_bad")
    assert (ex.termination_status, ex.error_origin) == ("ERROR", "preflight")
    assert ex.returncode == 1


def test_skip_schema_validation_env(tmp_path):
    a = GarudaAdapter(model_root=fake_model_root(tmp_path), julia=str(fake_julia(tmp_path)),
                      skip_schema_validation=True)
    a.run(BASE_CFG, tmp_path / "r")
    assert "GARUDA_SKIP_VALIDATION=1" in (tmp_path / "r" / "solver.log").read_text()


def test_julia_preflight_uses_the_models_gate(adapter, tmp_path):
    proc = adapter.julia_preflight(BASE_CFG, tmp_path / "pf")
    assert proc.returncode == 0
    assert GarudaAdapter._parse_status(proc.stdout, proc.returncode) == ("PREFLIGHT_OK", None)
    assert json.loads((tmp_path / "pf" / "config.json").read_text())["run_tag"] == "pf"


def test_default_validator_python_prefers_env(monkeypatch):
    from adapters.garuda.garuda_adapter import default_validator_python
    monkeypatch.setenv("GARUDA_PYTHON", "/x/python")
    assert default_validator_python() == "/x/python"
    assert GarudaAdapter(python_for_validator="/y/python").python_for_validator == "/y/python"


def test_command_shape(adapter):
    cmd = adapter._command(Path("/r/config.json"))
    assert cmd == [adapter.julia, f"--project={adapter.model_root}", "run_model.jl",
                   "--config", "/r/config.json"]


# --------------------------------------------------------------------------
# live-monitoring hook
# --------------------------------------------------------------------------

class _FakeMonitor:
    def __init__(self):
        self.lines, self.finished = [], None

    def on_line(self, line):
        self.lines.append(line)

    def finish(self, result):
        self.finished = result
        return {"n_lines": len(self.lines), "returncode": result.returncode}


def test_on_event_accepts_a_monitor_object(adapter, tmp_path):
    mon = _FakeMonitor()
    ex = adapter.run(BASE_CFG, tmp_path / "m", on_event=mon)
    assert ex.termination_status == "OPTIMAL"
    assert "".join(mon.lines) == (tmp_path / "m" / "solver.log").read_text()
    assert mon.finished.returncode == 0
    if hasattr(ex, "monitor"):
        assert ex.monitor == {"n_lines": len(mon.lines), "returncode": 0}


def test_on_event_callback_is_wrapped_in_a_run_monitor(adapter, tmp_path):
    pytest.importorskip("framework.monitor")
    events = []
    ex = adapter.run(BASE_CFG, tmp_path / "mon", on_event=events.append)
    assert ex.termination_status == "OPTIMAL"
    assert (tmp_path / "mon" / "monitor.json").exists()       # RunMonitor.finish persisted it


def test_broken_monitor_never_breaks_the_run(adapter, tmp_path):
    class Broken:
        def on_line(self, line):
            raise RuntimeError("boom")

        def finish(self, result):
            raise RuntimeError("boom")

    ex = adapter.run(BASE_CFG, tmp_path / "b", on_event=Broken())
    assert ex.termination_status == "OPTIMAL"


# --------------------------------------------------------------------------
# streaming helper
# --------------------------------------------------------------------------

def test_stream_local_tees_lines_in_order(tmp_path):
    child = ("import sys, time\n"
             "for i in range(3):\n"
             "    print('line', i); sys.stdout.flush(); time.sleep(0.01)\n"
             "print('to stderr', file=sys.stderr)\n"
             "sys.exit(3)\n")
    seen = []
    rc, wall = stream_local([sys.executable, "-c", child], cwd=tmp_path,
                            log_path=tmp_path / "log" / "solver.log", on_line=seen.append)
    assert rc == 3
    assert wall >= 0.03
    assert seen[:3] == ["line 0\n", "line 1\n", "line 2\n"]
    assert "to stderr\n" in seen                                # stderr merged
    assert (tmp_path / "log" / "solver.log").read_text() == "".join(seen)


# --------------------------------------------------------------------------
# describe_config / spec / registry
# --------------------------------------------------------------------------

def test_describe_config_mentions_everything_an_llm_needs():
    text = GarudaAdapter().describe_config()
    assert text == describe_schema()
    for key in KNOWN_KEYS:
        assert key in text, key
    for scenario in ("base", "grid", "village", "gridvillage", "highimportprice", "nocoal",
                     "captive", "gridcaptive"):
        assert scenario in text
    for word in ("Tier", "POLICY", "dispatch", "expansion", "highs", "gurobi", "time_limit",
                 "REGRESSION", "418.75012738446367", "889909.6973480765",
                 "205997.29702380873", "solved successfully", "reached the time limit",
                 "is infeasible", "did not solve", "WARNING: export_price"):
        assert word in text, word
    assert "island" in text and "year" in text


def test_intervention_spec_matches_module_constants():
    spec = GarudaAdapter().intervention_spec()
    assert spec.tier_a_keys == TIER_A_KEYS
    assert spec.tier_b_keys == TIER_B_KEYS
    assert spec.tier_c_keys == TIER_C_KEYS
    assert spec.allowed_values["lp_method"] == list(range(-1, 6))


def test_registry_resolves_garuda(tmp_path):
    a = get_adapter("garuda", model_root=tmp_path)
    assert isinstance(a, GarudaAdapter)
    assert a.name == "garuda"
    assert a.model_root == tmp_path.resolve()


# --------------------------------------------------------------------------
# regression headline helpers
# --------------------------------------------------------------------------

def _write_headline_csvs(d: Path, cost=418.75, co2=889909.7, nse=(100000.0, 105997.3)):
    d.mkdir(parents=True, exist_ok=True)
    (d / "cost_results.csv").write_text(f"Total_Costs,NSE_Costs\n{cost},1\n")
    (d / "clean_energy_results.csv").write_text(f"CO2_Emissions,Grid_REShare\n{co2},0\n")
    rows = "\n".join(f"{i},{v}" for i, v in enumerate(nse, 1))
    (d / "reliability_results.csv").write_text(f"Zone,Total_NSE_MWh\n{rows}\n")
    return {p.stem: p for p in d.glob("*.csv")}


def test_check_regression_headlines_within_tolerance(tmp_path):
    outputs = _write_headline_csvs(tmp_path)
    report = check_regression_headlines(outputs)
    assert set(report) == set(REGRESSION_HEADLINES)
    assert all(ok for _, _, ok in report.values()), report
    assert read_headline(outputs, "Total_NSE_MWh") == pytest.approx(205997.3)   # summed


def test_check_regression_headlines_flags_drift_and_missing(tmp_path):
    outputs = _write_headline_csvs(tmp_path, cost=430.0)        # +2.7 %
    report = check_regression_headlines(outputs)
    assert report["Total_Costs"][2] is False
    assert report["CO2_Emissions"][2] is True
    del outputs["reliability_results"]
    report = check_regression_headlines(outputs)
    assert report["Total_NSE_MWh"] == (None, REGRESSION_HEADLINES["Total_NSE_MWh"][2], False)
    (tmp_path / "cost_results.csv").write_text("Other\n1\n")
    assert read_headline(outputs, "Total_Costs") is None


# --------------------------------------------------------------------------
# remote backend with an in-memory transport
# --------------------------------------------------------------------------

class _FakeTransport(Transport):
    """Records the orchestration; 'runs' by returning canned output."""

    def __init__(self, stdout="Dispatch solved successfully (LP, UC relaxed).\n", rc=0):
        self.commands, self.pushed, self.pulled = [], [], []
        self.stdout, self.rc = stdout, rc

    def run(self, command, capture=True):
        self.commands.append(command)
        if command.startswith("mkdir"):
            return subprocess.CompletedProcess(command, 0, "", "")
        return subprocess.CompletedProcess(command, self.rc, self.stdout, "")

    def push(self, local_path, remote_path):
        self.pushed.append((local_path, remote_path))
        return subprocess.CompletedProcess("push", 0, "", "")

    def pull(self, remote_path, local_path):
        self.pulled.append((remote_path, local_path))
        Path(local_path).mkdir(parents=True, exist_ok=True)
        (Path(local_path) / "cost_results.csv").write_text("Total_Costs\n1\n")
        return subprocess.CompletedProcess("pull", 0, "", "")

    @property
    def description(self):
        return "fake"


def test_remote_adapter_orchestration(tmp_path):
    t = _FakeTransport()
    a = RemoteGarudaAdapter(remote_root="/srv/garuda/", transport=t, julia="/opt/julia/bin/julia",
                            remote_env={"GARUDA_SKIP_VALIDATION": "1"})
    assert a.remote_root == "/srv/garuda"
    assert a.name == "garuda-remote"
    assert a.validate_config(BASE_CFG).ok                         # schema only, no local inputs
    assert not a.validate_config(dict(BASE_CFG, scenario="nope")).ok

    run_dir = tmp_path / "iter00_remote"
    seen = []
    ex = a.run(BASE_CFG, run_dir, on_line=seen.append)

    assert ex.termination_status == "OPTIMAL" and ex.returncode == 0
    assert ex.error_origin is None
    executed = json.loads((run_dir / "config.json").read_text())
    assert executed["run_tag"] == "iter00_remote"
    job = "/srv/garuda/jobs/base_maluku_2030_reference__iter00_remote"
    assert t.commands[0] == f"mkdir -p {job}"
    assert t.pushed == [(str(run_dir / "config.json"), f"{job}/config.json")]
    run_cmd = t.commands[1]
    assert run_cmd.startswith("cd /srv/garuda && GARUDA_SKIP_VALIDATION=1 /opt/julia/bin/julia "
                              "--project=/srv/garuda run_model.jl --config")
    assert run_cmd.endswith(f"{job}/config.json")
    assert t.pulled == [("/srv/garuda/results/base_maluku_2030_reference__iter00_remote/",
                         str(run_dir / "outputs") + "/")]
    assert (run_dir / "solver.log").read_text().startswith("Dispatch solved successfully")
    assert "".join(seen) == (run_dir / "solver.log").read_text()
    assert set(a.locate_outputs(run_dir)) == {"cost_results"}


def test_remote_adapter_infeasible_and_transport_failure(tmp_path):
    t = _FakeTransport(stdout="Dispatch is infeasible (fleet cannot ...).\n", rc=1)
    a = RemoteGarudaAdapter(remote_root="/srv/garuda", transport=t)
    ex = a.run(BASE_CFG, tmp_path / "r1")
    assert (ex.termination_status, ex.error_origin) == ("INFEASIBLE", "solver")

    class Down(_FakeTransport):
        def push(self, local_path, remote_path):
            return subprocess.CompletedProcess("push", 255, "", "ssh: connection refused")

    ex = RemoteGarudaAdapter(remote_root="/srv/garuda", transport=Down()).run(BASE_CFG, tmp_path / "r2")
    assert (ex.termination_status, ex.error_origin) == ("ERROR", "remote")
    assert "connection refused" in (tmp_path / "r2" / "solver.log").read_text()


def test_remote_adapter_clone_command_and_construction():
    a = RemoteGarudaAdapter(remote_root="~/garuda", transport=_FakeTransport())
    cmd = a.clone_command()
    assert "git clone https://github.com/kaarthi19/garuda.git" in cmd
    assert " -b " not in cmd                                       # branch unset by default
    assert "bootstrap.jl" in cmd
    b = RemoteGarudaAdapter(remote_root="~/garuda", transport=_FakeTransport(), branch="dev")
    assert "git clone -b dev https://github.com/kaarthi19/garuda.git" in b.clone_command()
    with pytest.raises(ValueError):
        RemoteGarudaAdapter(remote_root="~/garuda")                # neither host nor transport
    loop = RemoteGarudaAdapter.local_loopback(remote_root="/tmp/g")
    assert loop.transport.description == "local"
    # a probe through the fake transport works without a network
    info = a.check_connection()
    assert info["transport"] == "fake" and info["returncode"] == 0
