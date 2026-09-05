"""Fixture-level checks that need no solver.

Every fixture under examples/garuda/fixtures carries a config plus a label in
the protocol's §5 schema. Without solving anything we can pin: the label schema
is complete and self-consistent, ``validate_config`` accepts exactly the
fixtures that are meant to reach the solver and rejects the preflight ones for
the planted reason, the labelled fix tier agrees with the adapter's
intervention spec, and the README lists every fixture. The infeasibility /
time-limit / anomaly fixtures themselves need a live solve (see
examples/garuda/demo_refine_loop.py and demo_supervisor.py).
"""

import json
from pathlib import Path

import pytest

from adapters.garuda import GarudaAdapter, REGRESSION_CONFIG, REGRESSION_HEADLINES

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "examples" / "garuda" / "fixtures"
ADAPTER = GarudaAdapter()
SPEC = ADAPTER.intervention_spec()

FIXTURE_NAMES = sorted(p.name for p in FIXTURES.iterdir() if (p / "config.json").is_file())

# The fixtures the build brief requires (extras are welcome, absences are not).
REQUIRED_FIXTURES = {
    "tierC_co2_floor", "tierC_re_floor_impossible", "tierA_time_limit",
    "tierB_storage_cap_zero", "preflight_bad_island", "preflight_illegal_scenario",
    "output_anomaly_export_arbitrage",
}

LABEL_KEYS = {
    "task_id", "family", "expected_status", "expected_error_origin",
    "expected_root_cause_category", "expected_tier", "expected_terminal_outcome",
    "planted_anomaly_metric", "needs_solver", "notes",
}
FAMILIES = {"tierA_fixable", "tierB_fixable", "tierC_infeasible", "preflight_error",
            "output_anomaly"}
STATUSES = {"OPTIMAL", "TIME_LIMIT", "INFEASIBLE", "ERROR"}
OUTCOMES = {"solved", "needs_human", "flagged"}


def _cfg(name):
    return json.loads((FIXTURES / name / "config.json").read_text())


def _label(name):
    return json.loads((FIXTURES / name / "expected.json").read_text())


def test_required_fixtures_exist():
    missing = REQUIRED_FIXTURES - set(FIXTURE_NAMES)
    assert not missing, f"missing fixtures: {sorted(missing)}"
    for name in FIXTURE_NAMES:
        assert (FIXTURES / name / "expected.json").is_file(), f"{name} has no expected.json"


@pytest.mark.parametrize("name", FIXTURE_NAMES)
def test_label_schema_is_complete_and_consistent(name):
    label = _label(name)
    missing = LABEL_KEYS - set(label)
    assert not missing, f"{name}: label missing {sorted(missing)}"
    assert label["task_id"] == f"garuda_{name}"
    assert label["family"] in FAMILIES
    assert label["expected_status"] in STATUSES
    assert label["expected_error_origin"] in {"preflight", "solver", "runtime", None}
    assert label["expected_tier"] in {"A", "B", "C", None}
    assert label["expected_terminal_outcome"] in OUTCOMES
    assert label["needs_solver"] in {"highs", "gurobi", None}
    assert isinstance(label["notes"], str) and label["notes"]

    fam = label["family"]
    if fam == "tierC_infeasible":
        assert label["expected_status"] == "INFEASIBLE"
        assert label["expected_error_origin"] == "solver"
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
    elif fam == "output_anomaly":
        assert label["expected_status"] == "OPTIMAL"
        # planted metric iff a root cause is planted; a clean baseline has neither
        assert (label["planted_anomaly_metric"] is None) == (
            label["expected_root_cause_category"] is None)


@pytest.mark.parametrize("name", FIXTURE_NAMES)
def test_validation_verdict_matches_label(name):
    """Preflight fixtures must be caught by validate_config for the planted
    reason; every other fixture must pass so its breakage is genuine."""
    label = _label(name)
    result = ADAPTER.validate_config(_cfg(name))
    if label["family"] == "preflight_error":
        assert not result.ok
        assert any(label["root_cause_contains"] in e for e in result.errors), result.errors
        # exactly the planted defect, nothing incidental
        assert len(result.errors) == 1, result.errors
    else:
        assert result.ok, result.errors


@pytest.mark.parametrize("name", FIXTURE_NAMES)
def test_gurobi_dependency_is_marked(name):
    cfg, label = _cfg(name), _label(name)
    if cfg.get("solver") == "gurobi":
        assert label["needs_solver"] == "gurobi", f"{name} uses Gurobi but is not marked"
    elif label["needs_solver"] is not None:
        assert label["needs_solver"] == "highs"


@pytest.mark.parametrize("name", FIXTURE_NAMES)
def test_fix_keys_agree_with_intervention_spec(name):
    """The labelled fix tier is what the framework would actually rule."""
    label = _label(name)
    keys = label.get("expected_fix_keys")
    if not keys or label["expected_tier"] is None:
        pytest.skip("no fix keys labelled")
    for key in keys:
        assert SPEC.tier_for_key(key).value == label["expected_tier"], (
            f"{name}: {key} is tier {SPEC.tier_for_key(key).value}, "
            f"label says {label['expected_tier']}")


def test_exactly_one_fixture_needs_gurobi():
    gurobi = [n for n in FIXTURE_NAMES if _label(n)["needs_solver"] == "gurobi"]
    assert gurobi == ["tierA_time_limit"]


def test_tier_c_fixtures_are_solver_bound_not_preflight():
    # Their breakage must be *genuine* infeasibility: valid configs, dispatch
    # engine (fast), HiGHS (no licence), no arbitrary key.
    for name in ("tierC_co2_floor", "tierC_re_floor_impossible"):
        cfg = _cfg(name)
        assert cfg["clean"] == "clean"
        assert cfg["engine"] == "dispatch"
        assert cfg["solver"] == "highs"
        assert ADAPTER.validate_config(cfg).ok


def test_regression_fixture_matches_adapter_constants():
    cfg = _cfg("baseline_maluku_dispatch")
    assert cfg == REGRESSION_CONFIG
    label = _label("baseline_maluku_dispatch")
    got = label["expected_headlines"]
    assert got["cost_results.Total_Costs"] == REGRESSION_HEADLINES["Total_Costs"][2]
    assert got["clean_energy_results.CO2_Emissions"] == REGRESSION_HEADLINES["CO2_Emissions"][2]
    assert got["reliability_results.Total_NSE_MWh(sum)"] == REGRESSION_HEADLINES["Total_NSE_MWh"][2]


def test_export_arbitrage_fixture_is_configured_as_an_arbitrage():
    cfg = _cfg("output_anomaly_export_arbitrage")
    assert cfg["export_price"] > cfg["import_price"]
    assert cfg["scenario"] in ("gridvillage", "gridcaptive", "highimportprice")


def test_readme_lists_every_fixture():
    readme = (FIXTURES / "README.md").read_text()
    for name in FIXTURE_NAMES:
        assert f"`{name}`" in readme, f"README.md does not list {name}"
