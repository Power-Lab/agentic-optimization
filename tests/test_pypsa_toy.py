"""Tests for the pypsa_toy adapter — the reproducible third leg of the study.

**No solver runs here.** Every test either works on the pure-Python schema, or
builds the pypsa network object and inspects its components, or drives
``PypsaToyAdapter.run`` against a *stub* runner script that prints the marker
lines a real run would print. That keeps the suite fast, deterministic and
runnable on an interpreter with no scientific stack: the pypsa-dependent tests
``importorskip("pypsa")``, everything else runs anywhere.

What is deliberately NOT covered: that the LP is feasible/infeasible as the
fixtures claim, and that the solve path through HiGHS returns what the runner
expects. Those need a solver; see ``examples/pypsa_toy/smoke_test.py``.
"""

from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from adapters.pypsa_toy import schema  # noqa: E402
from adapters.pypsa_toy.pypsa_adapter import (  # noqa: E402
    PypsaToyAdapter,
    default_python,
    describe_schema,
)
from framework.interventions import ProposedChange, Tier, decide  # noqa: E402
from framework.refine import apply_refinements  # noqa: E402
from framework.run_record import RunRecord  # noqa: E402

FIXTURES = REPO_ROOT / "examples" / "pypsa_toy" / "fixtures"
FIXTURE_DIRS = sorted(p for p in FIXTURES.iterdir() if p.is_dir()) if FIXTURES.is_dir() else []
FIXTURE_IDS = [p.name for p in FIXTURE_DIRS]


# ---------------------------------------------------------------- schema ----

def test_defaults_cover_every_known_key():
    assert set(schema.DEFAULTS) == set(schema.KNOWN_KEYS)
    assert len(schema.KEY_SPECS) == len(set(schema.KNOWN_KEYS)), "duplicate key spec"


def test_tiers_partition_the_key_set():
    a, b, c = schema.TIER_A_KEYS, schema.TIER_B_KEYS, schema.TIER_C_KEYS
    assert a | b | c == set(schema.KNOWN_KEYS)
    assert not (a & b) and not (a & c) and not (b & c)
    # The policy tier is the whole point: it must be exactly the three
    # constraints that decide what the study claims.
    assert c == {"co2_cap_t", "re_share_min", "allow_load_shedding"}


def test_empty_config_and_full_defaults_are_valid():
    assert schema.validate({}) == []
    assert schema.validate(dict(schema.DEFAULTS)) == []


def test_apply_defaults_overlays_user_keys():
    full = schema.apply_defaults({"demand_scale": 3.0})
    assert full["demand_scale"] == 3.0
    assert full["snapshots_days"] == schema.DEFAULTS["snapshots_days"]
    # unknown keys survive so validate() can complain about them
    assert schema.apply_defaults({"bogus": 1})["bogus"] == 1


def test_unknown_key_is_an_error_with_a_suggestion():
    errors = schema.validate({"co2_cap": 100.0})
    assert len(errors) == 1
    assert "Unknown config key 'co2_cap'" in errors[0]
    assert "did you mean 'co2_cap_t'" in errors[0]


def test_unknown_key_without_a_near_match_still_lists_legal_keys():
    errors = schema.validate({"totally_unrelated_setting": 1})
    assert errors and "legal keys:" in errors[0]


@pytest.mark.parametrize("config,fragment", [
    ({"demand_scale": "high"}, "must be a number"),
    ({"demand_scale": True}, "must be a number"),          # bool is not a number
    ({"snapshots_days": "seven"}, "must be an integer"),
    ({"allow_load_shedding": 1}, "must be true/false"),
    ({"solver": 3}, "must be a string"),
    ({"co2_cap_t": "none"}, "must be a number or null"),
    ({"co2_cap_t": float("nan")}, "must be finite"),
    ({"time_limit": float("inf")}, "must be finite"),
])
def test_type_errors(config, fragment):
    errors = schema.validate(config)
    assert errors, f"{config} should not validate"
    assert any(fragment in e for e in errors), errors


@pytest.mark.parametrize("config,fragment", [
    ({"co2_cap_t": -1.0}, "must be >= 0.0"),
    ({"re_share_min": 1.5}, "must be <= 1.0"),
    ({"re_share_min": -0.1}, "must be >= 0.0"),
    ({"demand_scale": 0.1}, "must be >= 0.25"),
    ({"demand_scale": 11.0}, "must be <= 10.0"),
    ({"time_limit": 0.0}, "must be > 0.0"),               # exclusive bound
    ({"threads": 0}, "must be >= 1"),
    ({"snapshots_days": 0}, "must be >= 1"),
    ({"snapshots_days": 15}, "must be <= 14"),
    ({"gas_price": -1.0}, "must be >= 0.0"),
])
def test_range_errors(config, fragment):
    errors = schema.validate(config)
    assert errors, f"{config} should not validate"
    assert any(fragment in e for e in errors), errors


def test_enum_error_names_the_legal_set():
    errors = schema.validate({"solver": "gurobi"})
    assert errors and "not in the legal set" in errors[0] and "highs" in errors[0]


def test_policy_keys_accept_null_meaning_no_constraint():
    assert schema.validate({"co2_cap_t": None, "re_share_min": None}) == []


def test_integral_float_accepted_for_int_key():
    # JSON round-trips 7 as 7.0; that must not be a validation error.
    assert schema.validate({"snapshots_days": 7.0, "threads": 2.0}) == []


def test_config_must_be_an_object():
    errors = schema.validate([1, 2, 3])  # type: ignore[arg-type]
    assert errors and "must be a JSON object" in errors[0]


def test_tier_table_is_complete():
    rows = schema.tier_table()
    assert len(rows) == len(schema.KNOWN_KEYS)
    assert {r[1] for r in rows} == {"A", "B", "C"}


# ------------------------------------------------------------ guardrail ----

def test_intervention_spec_mirrors_the_schema():
    spec = PypsaToyAdapter().intervention_spec()
    assert spec.tier_a_keys == schema.TIER_A_KEYS
    assert spec.tier_b_keys == schema.TIER_B_KEYS
    assert spec.tier_c_keys == schema.TIER_C_KEYS
    assert spec.allowed_values["solver"] == ["highs"]
    assert set(spec.allowed_values["allow_load_shedding"]) == {True, False}


@pytest.mark.parametrize("key", sorted({"co2_cap_t", "re_share_min", "allow_load_shedding"}))
def test_policy_keys_are_never_auto_applied(key):
    spec = PypsaToyAdapter().intervention_spec()
    after = {"co2_cap_t": 1e9, "re_share_min": 0.0, "allow_load_shedding": True}[key]
    decision = decide(ProposedChange(key=key, before=None, after=after), spec)
    assert decision.tier is Tier.C
    assert not decision.auto_apply
    assert decision.blocked_for_human


@pytest.mark.parametrize("key,after", [("time_limit", 1800.0), ("threads", 4), ("mip_gap", 0.05)])
def test_numeric_keys_auto_apply(key, after):
    spec = PypsaToyAdapter().intervention_spec()
    decision = decide(ProposedChange(key=key, before=None, after=after), spec)
    assert decision.tier is Tier.A and decision.auto_apply


@pytest.mark.parametrize("key,after", [("demand_scale", 0.5), ("line_expansion_allowed", True),
                                       ("gas_price", 25.0), ("snapshots_days", 3)])
def test_sanctioned_parameters_auto_apply_with_a_flag(key, after):
    spec = PypsaToyAdapter().intervention_spec()
    decision = decide(ProposedChange(key=key, before=None, after=after), spec)
    assert decision.tier is Tier.B and decision.auto_apply


def test_unknown_key_falls_back_to_tier_c():
    spec = PypsaToyAdapter().intervention_spec()
    decision = decide(ProposedChange(key="co2_cap", before=None, after=5000.0), spec)
    assert decision.tier is Tier.C and not decision.auto_apply


def test_out_of_enum_value_is_rejected_outright():
    spec = PypsaToyAdapter().intervention_spec()
    decision = decide(ProposedChange(key="solver", before="highs", after="gurobi"), spec)
    assert decision.rejected and not decision.auto_apply


def test_refiner_blocks_a_carbon_cap_relaxation_but_applies_the_time_limit():
    """The headline behaviour on this model: an agent may retune the solver, it
    may not quietly raise the cap that made the run infeasible."""
    spec = PypsaToyAdapter().intervention_spec()
    record = RunRecord(config={"co2_cap_t": 0.0, "time_limit": 60.0})
    outcome = apply_refinements(record, [
        ProposedChange(key="time_limit", before=60.0, after=600.0),
        ProposedChange(key="co2_cap_t", before=0.0, after=1e9),
    ], spec)
    assert outcome.next_config["time_limit"] == 600.0
    assert outcome.next_config["co2_cap_t"] == 0.0, "the cap must not move"
    assert [list(r.change)[0] for r in outcome.applied] == ["time_limit"]
    assert [list(r.change)[0] for r in outcome.blocked] == ["co2_cap_t"]
    assert outcome.needs_human
    # the audit trail keeps the blocked proposal, marked unapplied at tier C
    history = {list(r.change)[0]: r for r in record.refinement_history}
    assert history["time_limit"].applied is True and history["time_limit"].tier == "A"
    assert history["co2_cap_t"].applied is False and history["co2_cap_t"].tier == "C"


# --------------------------------------------------------------- adapter ----

def test_adapter_registers_itself():
    from framework.registry import get_adapter

    import adapters.pypsa_toy  # noqa: F401  (import triggers registration)

    adapter = get_adapter("pypsa_toy")
    assert isinstance(adapter, PypsaToyAdapter)
    assert adapter.name == "pypsa_toy"


def test_default_python_honours_the_env_var(monkeypatch):
    monkeypatch.setenv("PYPSA_PYTHON", "/some/where/python")
    assert default_python() == "/some/where/python"


def test_default_python_falls_back_to_something_runnable(monkeypatch):
    monkeypatch.delenv("PYPSA_PYTHON", raising=False)
    assert default_python()


def test_validate_config_flags_a_missing_interpreter(tmp_path):
    adapter = PypsaToyAdapter(python=str(tmp_path / "no-such-python"))
    result = adapter.validate_config({})
    assert not result.ok
    assert any("PYPSA_PYTHON" in e for e in result.errors)


def test_validate_config_flags_a_missing_runner(tmp_path):
    adapter = PypsaToyAdapter(python=sys.executable, runner=tmp_path / "gone.py")
    result = adapter.validate_config({})
    assert not result.ok
    assert any("runner script not found" in e for e in result.errors)


def test_locate_outputs_maps_stems_to_csvs(tmp_path):
    out = tmp_path / "outputs"
    out.mkdir()
    (out / "cost_results.csv").write_text("objective_usd\n1.0\n")
    (out / "emissions_results.csv").write_text("carrier,emissions_t\ntotal,5\n")
    (out / "notes.txt").write_text("ignored")
    outputs = PypsaToyAdapter().locate_outputs(tmp_path)
    assert set(outputs) == {"cost_results", "emissions_results"}
    assert outputs["cost_results"].name == "cost_results.csv"


def test_locate_outputs_is_empty_before_a_run(tmp_path):
    assert PypsaToyAdapter().locate_outputs(tmp_path) == {}


def test_describe_config_teaches_the_whole_schema():
    text = describe_schema()
    for key in schema.KNOWN_KEYS:
        assert key in text, f"describe_config never mentions {key}"
    # tiers, and which one is the policy tier
    assert "Tier A" in text and "Tier B" in text and "Tier C" in text
    assert "POLICY" in text
    # the physical facts a diagnosis needs
    assert "must-run" in text.lower()
    assert "1,289" in text or "1289" in text
    # the output files, so the analyzer knows what it can read
    for stem in ("generator_results", "storage_results", "line_results",
                 "cost_results", "emissions_results", "nse_results"):
        assert stem in text
    # the status contract
    assert "[pypsa_toy] status:" in text


def test_describe_config_is_the_adapters_description():
    assert PypsaToyAdapter().describe_config() == describe_schema()


# --------------------------------------------------------- status parsing ---

_parse = PypsaToyAdapter._parse_status


@pytest.mark.parametrize("status", ["OPTIMAL", "INFEASIBLE", "TIME_LIMIT", "ERROR"])
def test_parse_status_reads_the_marker_line(status):
    assert _parse(f"noise\n[pypsa_toy] status: {status}\n", 0)[0] == status


def test_parse_status_reads_the_error_origin():
    text = "[pypsa_toy] error_origin: preflight\n[pypsa_toy] status: ERROR\n"
    assert _parse(text, 2) == ("ERROR", "preflight")


def test_parse_status_keeps_solver_origin_for_infeasible():
    text = "[pypsa_toy] error_origin: solver\n[pypsa_toy] status: INFEASIBLE\n"
    assert _parse(text, 0) == ("INFEASIBLE", "solver")


def test_parse_status_clears_origin_for_successful_runs():
    text = "[pypsa_toy] error_origin: solver\n[pypsa_toy] status: OPTIMAL\n"
    assert _parse(text, 0) == ("OPTIMAL", None)


def test_parse_status_takes_the_last_marker():
    text = "[pypsa_toy] status: TIME_LIMIT\n[pypsa_toy] status: OPTIMAL\n"
    assert _parse(text, 0)[0] == "OPTIMAL"


def test_parse_status_ignores_a_bogus_marker_value():
    # falls through to the fallback tokens rather than inventing a status
    text = "[pypsa_toy] status: BANANA\nTermination condition: infeasible\n"
    assert _parse(text, 0) == ("INFEASIBLE", "solver")


@pytest.mark.parametrize("text,expected", [
    ("Termination condition: infeasible", ("INFEASIBLE", "solver")),
    ("Model   status      : Infeasible", ("INFEASIBLE", "solver")),
    ("Model   status      : Time limit reached", ("TIME_LIMIT", None)),
    ("Solving LP ...\nModel   status      : Optimal", ("OPTIMAL", None)),
    # An un-importable model package is an ENVIRONMENT failure, not a config
    # one: eval.cache.default_cacheable must refuse to cache it (a cached
    # ERROR would replay forever once pypsa is installed).
    ("ModuleNotFoundError: No module named 'pypsa'", ("ERROR", "environment")),
    ("Traceback (most recent call last):\n  ...\nValueError: nope", ("ERROR", "runtime")),
])
def test_parse_status_fallback_tokens(text, expected):
    assert _parse(text, 0) == expected


def test_parse_status_optimal_token_with_a_crash_is_an_error():
    # HiGHS said optimal, then the child died writing outputs.
    assert _parse("Model   status      : Optimal\n", 1) == ("ERROR", "runtime")


def test_parse_status_says_error_when_the_child_said_nothing():
    assert _parse("", 0) == ("ERROR", "runtime")
    assert _parse("random chatter\n", 137) == ("ERROR", "runtime")


# ------------------------------------------------------- runner internals ---

def test_condition_map_covers_the_linopy_conditions():
    from adapters.pypsa_toy import runner

    assert runner.CONDITION_TO_STATUS["optimal"] == "OPTIMAL"
    assert runner.CONDITION_TO_STATUS["infeasible"] == "INFEASIBLE"
    assert runner.CONDITION_TO_STATUS["infeasible_or_unbounded"] == "INFEASIBLE"
    assert runner.CONDITION_TO_STATUS["time_limit"] == "TIME_LIMIT"
    assert runner.CONDITION_TO_STATUS["terminated_by_limit"] == "TIME_LIMIT"
    # unbounded is deliberately absent: it means the model is wrong, not slow
    assert "unbounded" not in runner.CONDITION_TO_STATUS


@pytest.mark.parametrize("line,expected", [
    ("Model   status      : Optimal", "OPTIMAL"),
    ("Model   status      : Time limit reached", "TIME_LIMIT"),
    ("Model   status      : Infeasible", "INFEASIBLE"),
    ("Model   status      : Iteration limit reached", "TIME_LIMIT"),
    ("Model   status      : Unbounded or infeasible", "INFEASIBLE"),
    ("Objective value     :  1.23e+07", None),
    ("", None),
])
def test_status_from_highs_log(line, expected):
    from adapters.pypsa_toy import runner

    assert runner.status_from_highs_log(line) == expected


def test_status_from_highs_log_takes_the_final_model_status():
    from adapters.pypsa_toy import runner

    text = ("Model   status      : Not Set\n"
            "Solving...\n"
            "Model   status      : Time limit reached\n")
    assert runner.status_from_highs_log(text) == "TIME_LIMIT"


def test_runner_output_file_list_matches_what_it_documents():
    from adapters.pypsa_toy import runner

    assert set(runner.OUTPUT_FILES) == {
        "generator_results", "storage_results", "line_results",
        "cost_results", "emissions_results", "nse_results",
    }


# -------------------------------------------------------------- profiles ----

def test_horizon_summary_emissions_floor_scales_with_the_horizon():
    from adapters.pypsa_toy.network import horizon_summary

    one = horizon_summary({"snapshots_days": 1})
    three = horizon_summary({"snapshots_days": 3})
    assert one["hours"] == 24 and three["hours"] == 72
    # 300 MW x 20 % / 0.38 x 0.34 t/MWh_th x 24 h
    assert one["emissions_floor_t"] == pytest.approx(1288.4, abs=0.5)
    # both are rounded to 0.1 t by horizon_summary, hence the absolute tolerance
    assert three["emissions_floor_t"] == pytest.approx(3 * one["emissions_floor_t"], abs=0.2)


def test_horizon_summary_tracks_demand_scale():
    from adapters.pypsa_toy.network import horizon_summary

    base = horizon_summary({"snapshots_days": 2})
    doubled = horizon_summary({"snapshots_days": 2, "demand_scale": 2.0})
    assert doubled["demand_mwh"] == pytest.approx(2 * base["demand_mwh"], rel=1e-6)
    # the must-run floor does NOT move with demand — that is why a cap below it
    # cannot be escaped by any Tier-B change
    assert doubled["emissions_floor_t"] == base["emissions_floor_t"]


def test_profiles_are_deterministic_and_nested():
    from adapters.pypsa_toy.network import make_profiles

    a = make_profiles(14)
    b = make_profiles(14)
    assert (a["wind"] == b["wind"]).all()
    assert (a["demand"]["north"] == b["demand"]["north"]).all()
    # a 1-day horizon is a strict prefix of the 14-day series
    one = make_profiles(1)
    assert (one["wind"] == a["wind"][:24]).all()
    assert (one["solar"] == a["solar"][:24]).all()
    assert (one["demand"]["east"] == a["demand"]["east"][:24]).all()


def test_profiles_stay_inside_their_physical_bounds():
    from adapters.pypsa_toy.network import make_profiles

    p = make_profiles(14)
    assert 0.0 <= p["wind"].min() and p["wind"].max() <= 1.0
    assert 0.0 <= p["solar"].min() and p["solar"].max() <= 1.0
    for bus, peak in (("north", 500.0), ("east", 300.0), ("south", 200.0)):
        assert p["demand"][bus].min() > 0.0
        assert p["demand"][bus].max() < 1.3 * peak


@pytest.mark.parametrize("bad", [0, 15, -1])
def test_make_profiles_rejects_an_illegal_horizon(bad):
    from adapters.pypsa_toy.network import make_profiles

    with pytest.raises(ValueError):
        make_profiles(bad)


# ------------------------------------------------- network build (pypsa) ----

def _network(**config):
    pytest.importorskip("pypsa")
    from adapters.pypsa_toy.network import build_network

    return build_network(config)


def test_build_network_has_the_documented_components():
    n = _network(snapshots_days=1)
    assert list(n.buses.index) == ["north", "east", "south"]
    assert len(n.snapshots) == 24
    assert set(n.lines.index) == {"north-east", "north-south", "east-south"}
    assert {"coal_existing", "coal_new", "gas", "wind", "solar"} <= set(n.generators.index)
    assert set(n.storage_units.index) == {"battery_east", "battery_south"}
    assert set(n.loads.index) == {"load_north", "load_east", "load_south"}


def test_carrier_emission_factors():
    n = _network(snapshots_days=1)
    assert n.carriers.at["coal", "co2_emissions"] == pytest.approx(0.34)
    assert n.carriers.at["gas", "co2_emissions"] == pytest.approx(0.20)
    for zero in ("AC", "wind", "solar", "battery", "load_shedding"):
        assert n.carriers.at[zero, "co2_emissions"] == 0.0


def test_existing_coal_is_must_run_and_not_extendable():
    """The single fact the two Tier-C fixtures rest on."""
    n = _network(snapshots_days=1)
    gen = n.generators.loc["coal_existing"]
    assert gen.p_nom == 300.0
    assert bool(gen.p_nom_extendable) is False
    assert gen.p_min_pu == pytest.approx(0.20)


def test_renewable_potential_is_capped():
    n = _network(snapshots_days=1)
    assert n.generators.at["wind", "p_nom_max"] == 600.0
    assert n.generators.at["solar", "p_nom_max"] == 800.0
    assert bool(n.generators.at["wind", "p_nom_extendable"]) is True
    assert bool(n.generators.at["solar", "p_nom_extendable"]) is True


def test_load_shedding_generators_appear_only_when_allowed():
    off = _network(snapshots_days=1, allow_load_shedding=False)
    assert not [g for g in off.generators.index if g.startswith("shed_")]
    on = _network(snapshots_days=1, allow_load_shedding=True)
    shed = [g for g in on.generators.index if g.startswith("shed_")]
    assert sorted(shed) == ["shed_east", "shed_north", "shed_south"]
    assert on.generators.at["shed_north", "marginal_cost"] == pytest.approx(10_000.0)
    assert on.generators.at["shed_north", "carrier"] == "load_shedding"


def test_co2_cap_becomes_a_global_constraint():
    without = _network(snapshots_days=1)
    assert len(without.global_constraints) == 0
    with_cap = _network(snapshots_days=1, co2_cap_t=1234.0)
    row = with_cap.global_constraints.loc["co2_cap"]
    assert row.sense == "<="
    assert row.constant == pytest.approx(1234.0)
    assert row.carrier_attribute == "co2_emissions"


def test_line_expansion_toggle_controls_the_ratings():
    free = _network(snapshots_days=1, line_expansion_allowed=True)
    assert bool(free.lines.at["north-east", "s_nom_extendable"]) is True
    assert free.lines.at["north-east", "s_nom_max"] == 20_000.0
    assert free.lines.at["north-east", "s_nom_min"] == 400.0
    frozen = _network(snapshots_days=1, line_expansion_allowed=False)
    assert bool(frozen.lines.at["north-east", "s_nom_extendable"]) is False
    assert frozen.lines.at["north-east", "s_nom_max"] == 400.0
    assert frozen.lines.at["north-east", "capital_cost"] == 0.0
    # east's import ceiling with the lines frozen — the tierB_demand_scale fixture
    assert frozen.lines.at["north-east", "s_nom"] + frozen.lines.at["east-south", "s_nom"] == 600.0


def test_demand_scale_multiplies_every_bus():
    base = _network(snapshots_days=1)
    scaled = _network(snapshots_days=1, demand_scale=2.5)
    for load in ("load_north", "load_east", "load_south"):
        ratio = scaled.loads_t.p_set[load] / base.loads_t.p_set[load]
        assert ratio.round(9).nunique() == 1
        assert float(ratio.iloc[0]) == pytest.approx(2.5)


def test_snapshots_days_controls_the_horizon():
    for days in (1, 3, 14):
        assert len(_network(snapshots_days=days).snapshots) == days * 24


def test_capital_costs_are_prorated_to_the_horizon():
    one = _network(snapshots_days=1)
    fourteen = _network(snapshots_days=14)
    assert one.generators.at["wind", "capital_cost"] == pytest.approx(110_000.0 * 24 / 8760)
    assert fourteen.generators.at["wind", "capital_cost"] == pytest.approx(
        14 * one.generators.at["wind", "capital_cost"])
    # and the config's capex key drives it
    cheap = _network(snapshots_days=1, wind_capex=55_000.0)
    assert cheap.generators.at["wind", "capital_cost"] == pytest.approx(
        one.generators.at["wind", "capital_cost"] / 2)


def test_fuel_prices_reach_the_marginal_costs():
    n = _network(snapshots_days=1, gas_price=0.0, coal_price=10.0)
    # gas: price/efficiency + VOM = 0/0.55 + 2
    assert n.generators.at["gas", "marginal_cost"] == pytest.approx(2.0)
    assert n.generators.at["coal_existing", "marginal_cost"] == pytest.approx(10 / 0.38 + 3.0)


def test_battery_round_trip_efficiency():
    n = _network(snapshots_days=1)
    su = n.storage_units.loc["battery_east"]
    assert su.max_hours == 4.0
    assert su.efficiency_store * su.efficiency_dispatch == pytest.approx(0.9025)
    assert bool(su.cyclic_state_of_charge) is True


def test_rebuilding_the_same_config_gives_the_same_numbers():
    a = _network(snapshots_days=2, demand_scale=1.3)
    b = _network(snapshots_days=2, demand_scale=1.3)
    assert (a.loads_t.p_set.values == b.loads_t.p_set.values).all()
    assert (a.generators_t.p_max_pu.values == b.generators_t.p_max_pu.values).all()


def test_network_meta_carries_the_config_and_horizon_facts():
    n = _network(snapshots_days=1, co2_cap_t=9.0)
    assert n.meta["adapter"] == "pypsa_toy"
    assert n.meta["config"]["co2_cap_t"] == 9.0
    assert n.meta["config"]["snapshots_days"] == 1
    assert n.meta["horizon"]["emissions_floor_t"] > 0


def test_re_share_floor_becomes_a_linopy_constraint():
    """Builds the LP in memory and adds the RE-share row. Nothing is solved:
    ``create_model`` only assembles the constraint matrix."""
    pytest.importorskip("pypsa")
    from adapters.pypsa_toy.network import re_share_extra_functionality

    n = _network(snapshots_days=1)
    model = n.optimize.create_model()
    assert "Generator-p" in model.variables
    re_share_extra_functionality(0.4)(n, n.snapshots)
    assert "re_share_min" in n.model.constraints
    constraint = n.model.constraints["re_share_min"]
    # one term per (snapshot, generator) on each side: 24 h x (2 RE + 5 total)
    assert constraint.nterm == 24 * (2 + 5)


# --------------------------------------------------------- run() plumbing ---

_STUB_TEMPLATE = '''#!/usr/bin/env python
"""Stub stand-in for adapters/pypsa_toy/runner.py. Prints the marker lines a
real run prints and writes the CSVs a real run writes. No solver involved."""
import argparse, pathlib, sys

ap = argparse.ArgumentParser()
ap.add_argument("--config", required=True)
ap.add_argument("--run-dir", required=True)
args = ap.parse_args()

run_dir = pathlib.Path(args.run_dir)
print("[pypsa_toy] runner start")
print("[pypsa_toy] horizon emissions_floor_t = 1288.4")
{body}
'''

_STUB_BODIES = {
    "optimal": '''
out = run_dir / "outputs"
out.mkdir(parents=True, exist_ok=True)
(out / "cost_results.csv").write_text("objective_usd,emissions_t\\n42.0,1000.0\\n")
(out / "nse_results.csv").write_text("bus,shed_mwh\\ntotal,0.0\\n")
print("[pypsa_toy] linopy status: ok / termination condition: optimal")
print("[pypsa_toy] status: OPTIMAL")
sys.exit(0)
''',
    "infeasible": '''
print("[pypsa_toy] error_origin: solver")
print("[pypsa_toy] status: INFEASIBLE")
sys.exit(0)
''',
    "crash": '''
print("Traceback (most recent call last):")
print("ValueError: the stub exploded")
sys.exit(1)
''',
}


def _write_stub(tmp_path: Path, kind: str) -> Path:
    stub = tmp_path / f"stub_runner_{kind}.py"
    stub.write_text(_STUB_TEMPLATE.format(body=_STUB_BODIES[kind]))
    stub.chmod(stub.stat().st_mode | stat.S_IEXEC)
    return stub


def _adapter(tmp_path: Path, kind: str) -> PypsaToyAdapter:
    return PypsaToyAdapter(python=sys.executable, runner=_write_stub(tmp_path, kind))


def test_run_writes_the_config_and_tees_the_log(tmp_path):
    run_dir = tmp_path / "run"
    execution = _adapter(tmp_path, "optimal").run({"snapshots_days": 1}, run_dir)

    assert json.loads((run_dir / "config.json").read_text()) == {"snapshots_days": 1}
    log = (run_dir / "solver.log").read_text()
    assert "[pypsa_toy] runner start" in log
    assert "[pypsa_toy] status: OPTIMAL" in log
    assert execution.solver_log == "solver.log"
    assert execution.termination_status == "OPTIMAL"
    assert execution.error_origin is None
    assert execution.returncode == 0
    assert execution.wall_seconds is not None and execution.wall_seconds >= 0


def test_run_archives_outputs_where_locate_outputs_finds_them(tmp_path):
    run_dir = tmp_path / "run"
    adapter = _adapter(tmp_path, "optimal")
    adapter.run({}, run_dir)
    outputs = adapter.locate_outputs(run_dir)
    assert set(outputs) == {"cost_results", "nse_results"}
    assert "objective_usd" in outputs["cost_results"].read_text()


def test_run_reports_infeasible_without_raising(tmp_path):
    execution = _adapter(tmp_path, "infeasible").run({}, tmp_path / "run")
    assert execution.termination_status == "INFEASIBLE"
    assert execution.error_origin == "solver"
    assert execution.returncode == 0


def test_run_reports_a_crash_as_a_runtime_error(tmp_path):
    execution = _adapter(tmp_path, "crash").run({}, tmp_path / "run")
    assert execution.termination_status == "ERROR"
    assert execution.error_origin == "runtime"
    assert execution.returncode == 1


def test_run_streams_every_line_to_on_line(tmp_path):
    seen = []
    _adapter(tmp_path, "optimal").run({}, tmp_path / "run", on_line=seen.append)
    joined = "".join(seen)
    assert "[pypsa_toy] runner start" in joined
    assert "[pypsa_toy] status: OPTIMAL" in joined
    # the callback saw the same text the log file kept
    assert joined == (tmp_path / "run" / "solver.log").read_text()


def test_run_feeds_the_live_monitor_when_one_is_supplied(tmp_path):
    """``on_event`` may be a ready-made monitor object; the adapter must feed it
    every line and must not blow up if it misbehaves."""
    class _Monitor:
        def __init__(self):
            self.lines = []

        def on_line(self, line):
            self.lines.append(line)

        def finish(self, result):
            return {"n_lines": len(self.lines), "returncode": result.returncode}

    monitor = _Monitor()
    execution = _adapter(tmp_path, "optimal").run({}, tmp_path / "run", on_event=monitor)
    assert any("status: OPTIMAL" in line for line in monitor.lines)
    if hasattr(execution, "monitor") and execution.monitor is not None:
        assert execution.monitor["n_lines"] == len(monitor.lines)


def test_a_broken_monitor_never_fails_the_run(tmp_path):
    class _Exploding:
        def on_line(self, line):
            raise RuntimeError("monitoring bug")

        def finish(self, result):
            raise RuntimeError("still broken")

    execution = _adapter(tmp_path, "optimal").run({}, tmp_path / "run", on_event=_Exploding())
    assert execution.termination_status == "OPTIMAL"


def test_run_puts_the_repo_on_the_child_pythonpath(tmp_path):
    env = PypsaToyAdapter()._env()
    assert str(REPO_ROOT) in env["PYTHONPATH"].split(os.pathsep)
    assert env["PYTHONUNBUFFERED"] == "1"


# --------------------------------------------------------------- fixtures ---

def test_there_are_fixtures():
    assert FIXTURE_DIRS, f"no fixtures under {FIXTURES}"


@pytest.mark.parametrize("fixture", FIXTURE_DIRS, ids=FIXTURE_IDS)
def test_fixture_files_are_well_formed(fixture):
    config = json.loads((fixture / "config.json").read_text())
    expected = json.loads((fixture / "expected.json").read_text())
    assert isinstance(config, dict)
    assert expected["task_id"] == f"pypsa_toy_{fixture.name}"
    assert expected["family"] in {"tierA_fixable", "tierB_fixable", "tierC_infeasible",
                                  "preflight_error", "output_anomaly"}
    assert expected["expected_status"] in {"OPTIMAL", "INFEASIBLE", "TIME_LIMIT", "ERROR"}
    assert expected["expected_terminal_outcome"] in {"solved", "needs_human", "flagged"}
    assert expected["needs_solver"] in {"highs", "none"}
    assert expected["notes"].strip()


@pytest.mark.parametrize("fixture", FIXTURE_DIRS, ids=FIXTURE_IDS)
def test_fixture_validation_verdict_matches_its_label(fixture):
    """Without a solver we can still falsify half of every label: a fixture
    whose break is a preflight error must fail validate_config, and one whose
    break only shows up in the solve must pass it."""
    config = json.loads((fixture / "config.json").read_text())
    expected = json.loads((fixture / "expected.json").read_text())
    result = PypsaToyAdapter(python=sys.executable).validate_config(config)
    if expected["expected_error_origin"] == "preflight":
        assert not result.ok, "labelled a preflight error but validates cleanly"
        contains = expected.get("root_cause_contains")
        if contains:
            assert any(contains.lower() in e.lower() for e in result.errors), result.errors
    else:
        assert result.ok, f"expected a runnable config, got {result.errors}"


@pytest.mark.parametrize("fixture", FIXTURE_DIRS, ids=FIXTURE_IDS)
def test_fixture_fix_keys_are_real_keys_in_the_labelled_tier(fixture):
    expected = json.loads((fixture / "expected.json").read_text())
    tier = expected.get("expected_tier")
    spec = PypsaToyAdapter().intervention_spec()
    for key in expected.get("expected_fix_keys", []):
        if key not in schema.KNOWN_KEYS:
            # only legal for the unknown-key fixture, where the point is that
            # the framework must not guess
            assert expected["expected_root_cause_category"] == "config_unknown_key"
            continue
        if tier in {"A", "B", "C"} and expected["expected_root_cause_category"] != "config_unknown_key":
            assert spec.tier_for_key(key).value == tier, (
                f"{fixture.name}: {key} is tier {spec.tier_for_key(key).value}, label says {tier}")


@pytest.mark.parametrize("fixture", FIXTURE_DIRS, ids=FIXTURE_IDS)
def test_fixture_must_not_apply_keys_are_all_policy_keys(fixture):
    """A key the agent is forbidden to touch must actually be Tier C — otherwise
    the fixture is asking the guardrail to do something it never promised."""
    expected = json.loads((fixture / "expected.json").read_text())
    spec = PypsaToyAdapter().intervention_spec()
    for key in expected.get("must_not_apply_keys", []):
        assert spec.tier_for_key(key) is Tier.C, f"{fixture.name}: {key} is not Tier C"


@pytest.mark.parametrize("fixture", FIXTURE_DIRS, ids=FIXTURE_IDS)
def test_tier_c_fixtures_demand_a_human(fixture):
    expected = json.loads((fixture / "expected.json").read_text())
    if expected["family"] == "tierC_infeasible":
        assert expected["expected_tier"] == "C"
        assert expected["expected_terminal_outcome"] == "needs_human"
        assert expected["expected_status"] == "INFEASIBLE"


def test_readme_documents_every_fixture():
    readme = (FIXTURES / "README.md").read_text()
    for fixture in FIXTURE_DIRS:
        assert f"`{fixture.name}`" in readme, f"{fixture.name} is missing from the README"


def test_the_planted_anomaly_names_a_column_the_runner_writes():
    from adapters.pypsa_toy import runner

    for fixture in FIXTURE_DIRS:
        expected = json.loads((fixture / "expected.json").read_text())
        metric = expected.get("planted_anomaly_metric")
        if not metric:
            continue
        stem, _, column = metric.partition(".")
        assert stem in runner.OUTPUT_FILES, f"{metric} names no known output file"
        assert column, f"{metric} names no column"


# ------------------------------------- output writing (no solver involved) ---

def _fake_solution(n, shed_mw: float = 0.0):
    """Decorate a freshly built network with the attributes ``n.optimize`` would
    set, so ``runner.write_outputs`` can be exercised without a solver.

    The numbers are arbitrary but internally consistent (dispatch never exceeds
    the installed capacity), which is all the CSV writer cares about.
    """
    import numpy as np
    import pandas as pd

    idx = n.snapshots
    gens = n.generators

    p_nom_opt = gens.p_nom.copy().astype(float)
    dispatch = {}
    for name, row in gens.iterrows():
        if name == "coal_existing":
            p_nom_opt[name] = 300.0
            series = np.full(len(idx), 120.0)
        elif name == "gas":
            p_nom_opt[name] = 400.0
            series = np.linspace(50.0, 400.0, len(idx))
        elif name == "coal_new":
            p_nom_opt[name] = 0.0
            series = np.zeros(len(idx))
        elif name == "wind":
            p_nom_opt[name] = 500.0
            series = 500.0 * n.generators_t.p_max_pu[name].to_numpy() * 0.8
        elif name == "solar":
            p_nom_opt[name] = 300.0
            series = 300.0 * n.generators_t.p_max_pu[name].to_numpy()
        else:  # shed_<bus>
            p_nom_opt[name] = row.p_nom
            series = np.full(len(idx), shed_mw)
        dispatch[name] = series
    n.generators["p_nom_opt"] = p_nom_opt
    n.generators_t.p = pd.DataFrame(dispatch, index=idx)[gens.index]

    su = n.storage_units
    n.storage_units["p_nom_opt"] = pd.Series(100.0, index=su.index)
    sign = np.where(np.arange(len(idx)) % 2 == 0, 1.0, -1.0)
    n.storage_units_t.p = pd.DataFrame(
        {name: 40.0 * sign for name in su.index}, index=idx)
    n.storage_units_t.state_of_charge = pd.DataFrame(
        {name: np.full(len(idx), 150.0) for name in su.index}, index=idx)

    n.lines["s_nom_opt"] = n.lines.s_nom.astype(float) * 1.5
    n.lines_t.p0 = pd.DataFrame(
        {name: np.linspace(-100.0, 100.0, len(idx)) for name in n.lines.index}, index=idx)

    if len(n.global_constraints):
        n.global_constraints["mu"] = 12.5
    n._objective = 1_234_567.0
    n._objective_constant = 0.0
    return n


def _write_outputs(tmp_path, config, shed_mw=0.0):
    pytest.importorskip("pypsa")
    from adapters.pypsa_toy import runner
    from adapters.pypsa_toy.network import build_network

    n = _fake_solution(build_network(config), shed_mw=shed_mw)
    out = tmp_path / "outputs"
    headline = runner.write_outputs(n, schema.apply_defaults(config), out, 1.23)
    return out, headline


def _rows(path: Path):
    import csv

    with path.open() as fh:
        return list(csv.DictReader(fh))


def test_write_outputs_produces_every_documented_file(tmp_path):
    from adapters.pypsa_toy import runner

    out, _ = _write_outputs(tmp_path, {"snapshots_days": 1, "co2_cap_t": 50_000.0})
    written = sorted(p.stem for p in out.glob("*.csv"))
    assert written == sorted(runner.OUTPUT_FILES)


def test_generator_results_columns_and_arithmetic(tmp_path):
    out, _ = _write_outputs(tmp_path, {"snapshots_days": 1})
    rows = {r["generator"]: r for r in _rows(out / "generator_results.csv")}
    assert set(rows) >= {"coal_existing", "coal_new", "gas", "wind", "solar"}
    coal = rows["coal_existing"]
    # 120 MW for 24 h
    assert float(coal["energy_mwh"]) == pytest.approx(2880.0)
    # emissions = energy / efficiency x carrier factor
    assert float(coal["emissions_t"]) == pytest.approx(2880.0 / 0.38 * 0.34)
    assert float(coal["capacity_factor"]) == pytest.approx(120.0 / 300.0)
    # renewables get an availability/curtailment number, thermals do not
    assert float(rows["wind"]["curtailment_mwh"]) > 0
    assert rows["coal_new"]["available_mwh"] in ("", "nan", "NaN")
    shares = sum(float(r["energy_share"]) for r in rows.values())
    assert shares == pytest.approx(1.0)


def test_cost_results_is_one_row_of_headline_numbers(tmp_path):
    out, headline = _write_outputs(tmp_path, {"snapshots_days": 1})
    rows = _rows(out / "cost_results.csv")
    assert len(rows) == 1
    row = rows[0]
    for key in ("objective_usd", "capital_cost_usd", "variable_cost_usd",
                "shedding_cost_usd", "total_system_cost_usd", "emissions_t",
                "re_share_of_generation", "gas_energy_share", "coal_energy_share",
                "shed_share_of_demand", "snapshots", "solve_seconds"):
        assert key in row, f"cost_results is missing {key}"
    assert float(row["objective_usd"]) == pytest.approx(1_234_567.0)
    assert int(float(row["snapshots"])) == 24
    assert float(row["total_system_cost_usd"]) == pytest.approx(
        float(row["capital_cost_usd"]) + float(row["variable_cost_usd"])
        + float(row["shedding_cost_usd"]))
    assert headline["gas_energy_share"] == pytest.approx(float(row["gas_energy_share"]))


def test_carrier_shares_sum_to_one(tmp_path):
    out, _ = _write_outputs(tmp_path, {"snapshots_days": 1})
    row = _rows(out / "cost_results.csv")[0]
    total = (float(row["gas_energy_share"]) + float(row["coal_energy_share"])
             + float(row["re_share_of_generation"]))
    assert total == pytest.approx(1.0)


def test_emissions_results_totals_and_cap_accounting(tmp_path):
    out, _ = _write_outputs(tmp_path, {"snapshots_days": 1, "co2_cap_t": 50_000.0})
    rows = {r["carrier"]: r for r in _rows(out / "emissions_results.csv")}
    assert "total" in rows
    per_carrier = sum(float(r["emissions_t"]) for k, r in rows.items() if k != "total")
    assert float(rows["total"]["emissions_t"]) == pytest.approx(per_carrier)
    assert float(rows["total"]["co2_cap_t"]) == 50_000.0
    assert float(rows["total"]["cap_shadow_price_usd_per_t"]) == pytest.approx(12.5)
    assert float(rows["wind"]["emissions_t"]) == 0.0


def test_emissions_results_leaves_cap_columns_blank_without_a_cap(tmp_path):
    out, _ = _write_outputs(tmp_path, {"snapshots_days": 1})
    total = [r for r in _rows(out / "emissions_results.csv") if r["carrier"] == "total"][0]
    assert total["co2_cap_t"] == ""
    assert total["cap_utilisation"] == ""


def test_nse_results_reports_zero_shedding_when_it_is_disallowed(tmp_path):
    out, _ = _write_outputs(tmp_path, {"snapshots_days": 1, "allow_load_shedding": False})
    rows = {r["bus"]: r for r in _rows(out / "nse_results.csv")}
    assert set(rows) == {"north", "east", "south", "total"}
    assert float(rows["total"]["shed_mwh"]) == 0.0
    assert float(rows["total"]["shed_share"]) == 0.0
    assert float(rows["total"]["demand_mwh"]) == pytest.approx(
        sum(float(rows[b]["demand_mwh"]) for b in ("north", "east", "south")))
    assert float(rows["total"]["peak_demand_mw"]) > float(rows["north"]["peak_demand_mw"])


def test_nse_results_accounts_for_shedding_when_it_happens(tmp_path):
    out, headline = _write_outputs(
        tmp_path, {"snapshots_days": 1, "allow_load_shedding": True}, shed_mw=25.0)
    rows = {r["bus"]: r for r in _rows(out / "nse_results.csv")}
    # 25 MW shed at each of three buses for 24 h
    assert float(rows["total"]["shed_mwh"]) == pytest.approx(3 * 25.0 * 24)
    assert float(rows["north"]["shed_hours"]) == 24
    assert 0 < float(rows["total"]["shed_share"]) < 1
    assert headline["shed_share_of_demand"] == pytest.approx(
        float(rows["total"]["shed_share"]))
    # shedding is priced at VOLL and shows up as its own cost line
    cost = _rows(out / "cost_results.csv")[0]
    assert float(cost["shedding_cost_usd"]) == pytest.approx(3 * 25.0 * 24 * 10_000.0)


def test_shedding_is_excluded_from_generation_shares(tmp_path):
    out, _ = _write_outputs(
        tmp_path, {"snapshots_days": 1, "allow_load_shedding": True}, shed_mw=25.0)
    row = _rows(out / "cost_results.csv")[0]
    total = (float(row["gas_energy_share"]) + float(row["coal_energy_share"])
             + float(row["re_share_of_generation"]))
    assert total == pytest.approx(1.0), "load shedding must not count as generation"


def test_line_results_reports_expansion_and_utilisation(tmp_path):
    out, _ = _write_outputs(tmp_path, {"snapshots_days": 1, "line_expansion_allowed": True})
    rows = {r["line"]: r for r in _rows(out / "line_results.csv")}
    assert set(rows) == {"north-east", "north-south", "east-south"}
    ne = rows["north-east"]
    assert float(ne["s_nom_existing_mw"]) == 400.0
    assert float(ne["s_nom_opt_mw"]) == pytest.approx(600.0)
    assert float(ne["expansion_mw"]) == pytest.approx(200.0)
    assert float(ne["max_abs_flow_mw"]) == pytest.approx(100.0)
    assert 0 <= float(ne["utilisation_max"]) <= 1


def test_storage_results_reports_energy_capacity_and_cycles(tmp_path):
    out, _ = _write_outputs(tmp_path, {"snapshots_days": 1})
    rows = {r["storage_unit"]: r for r in _rows(out / "storage_results.csv")}
    assert set(rows) == {"battery_east", "battery_south"}
    east = rows["battery_east"]
    assert float(east["p_nom_opt_mw"]) == pytest.approx(100.0)
    assert float(east["energy_capacity_mwh"]) == pytest.approx(400.0)  # 100 MW x 4 h
    assert float(east["discharged_mwh"]) == pytest.approx(12 * 40.0)
    assert float(east["charged_mwh"]) == pytest.approx(12 * 40.0)
    assert float(east["full_cycles"]) > 0


def test_write_outputs_is_stable_across_horizons(tmp_path):
    """The columns must not depend on the horizon — the analyzer reads the same
    schema whether the run was one day or fourteen."""
    one, _ = _write_outputs(tmp_path / "a", {"snapshots_days": 1})
    three, _ = _write_outputs(tmp_path / "b", {"snapshots_days": 3})
    for stem in ("generator_results", "cost_results", "nse_results",
                 "line_results", "storage_results", "emissions_results"):
        cols_one = _rows(one / f"{stem}.csv")[0].keys()
        cols_three = _rows(three / f"{stem}.csv")[0].keys()
        assert list(cols_one) == list(cols_three), stem


def test_the_carbon_cap_actually_enters_the_lp():
    """A GlobalConstraint row in the dataframe is not the same thing as a row in
    the constraint matrix. Assemble the LP (no solve) and check it is there —
    the two Tier-C fixtures are meaningless if the cap is decorative."""
    pytest.importorskip("pypsa")
    uncapped = _network(snapshots_days=1)
    uncapped.optimize.create_model()
    assert not [c for c in uncapped.model.constraints if "GlobalConstraint" in c]

    capped = _network(snapshots_days=1, co2_cap_t=5000.0)
    capped.optimize.create_model()
    assert "GlobalConstraint-co2_cap" in list(capped.model.constraints)


def test_freezing_the_lines_removes_the_expansion_variable():
    """The tierB_demand_scale fixture depends on line capacity being fixed, not
    merely expensive."""
    pytest.importorskip("pypsa")
    free = _network(snapshots_days=1, line_expansion_allowed=True)
    free.optimize.create_model()
    assert "Line-s_nom" in list(free.model.variables)

    frozen = _network(snapshots_days=1, line_expansion_allowed=False)
    frozen.optimize.create_model()
    assert "Line-s_nom" not in list(frozen.model.variables)


def test_the_built_network_passes_pypsas_own_consistency_check():
    pytest.importorskip("pypsa")
    for config in ({"snapshots_days": 1},
                   {"snapshots_days": 1, "co2_cap_t": 5000.0, "allow_load_shedding": True},
                   {"snapshots_days": 2, "line_expansion_allowed": False, "demand_scale": 4.0}):
        _network(**config).consistency_check(strict=["unknown_buses"])


# --------------------------------------------- runner preflight (no solver) --

def test_runner_rejects_an_invalid_config_before_touching_pypsa(tmp_path, capsys):
    """The schema gate runs before ``import pypsa``, so a bad config fails fast
    and identically on any interpreter."""
    from adapters.pypsa_toy import runner

    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"co2_cap": 5000.0}))
    code = runner.main(["--config", str(cfg), "--run-dir", str(tmp_path / "run")])
    out = capsys.readouterr().out
    assert code == 2
    assert "[pypsa_toy] preflight error: Unknown config key 'co2_cap'" in out
    assert "[pypsa_toy] error_origin: preflight" in out
    assert "[pypsa_toy] status: ERROR" in out


def test_runner_reports_an_unreadable_config(tmp_path, capsys):
    from adapters.pypsa_toy import runner

    code = runner.main(["--config", str(tmp_path / "nope.json"),
                        "--run-dir", str(tmp_path / "run")])
    out = capsys.readouterr().out
    assert code == 2
    assert "cannot read config" in out
    assert "[pypsa_toy] status: ERROR" in out


def test_runner_clears_stale_results_from_a_reused_run_dir(tmp_path, capsys):
    """A rerun into an existing directory must not inherit the previous run's
    CSVs — an infeasible run wearing an optimal run's outputs is the worst kind
    of wrong answer."""
    from adapters.pypsa_toy import runner

    run_dir = tmp_path / "run"
    stale_out = run_dir / "outputs"
    stale_out.mkdir(parents=True)
    (stale_out / "cost_results.csv").write_text("objective_usd\n999.0\n")
    (run_dir / "highs.log").write_text("Model   status      : Optimal\n")
    (run_dir / "summary.json").write_text('{"status": "OPTIMAL"}')

    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"co2_cap": 5000.0}))          # fails preflight
    runner.main(["--config", str(cfg), "--run-dir", str(run_dir)])
    capsys.readouterr()

    assert not stale_out.exists()
    assert not (run_dir / "highs.log").exists()
    assert not (run_dir / "summary.json").exists()


@pytest.mark.parametrize("config,fragment", [
    ({"co2_cap_t": 0.0, "snapshots_days": 3},
     "co2_cap_t = 0.0 t is below the must-run coal emissions floor"),
    ({"re_share_min": 1.0, "snapshots_days": 3},
     "re_share_min = 1.0 exceeds the wind + solar technical potential"),
    ({"gas_price": 0.0}, "gas_price = 0"),
    ({"coal_price": 0.0}, "coal_price = 0"),
    ({"allow_load_shedding": False}, "every MWh of demand must be served"),
])
def test_preflight_notes_name_the_physical_limit_that_was_crossed(config, fragment):
    """These notes are the analyzer's shortcut to the right diagnosis, so each
    has to fire on exactly the config that needs it. Pure function — no solve."""
    from adapters.pypsa_toy import runner
    from adapters.pypsa_toy.network import horizon_summary

    cfg = schema.apply_defaults(config)
    notes = runner.preflight_notes(cfg, horizon_summary(cfg))
    assert any(fragment in n for n in notes), notes


@pytest.mark.parametrize("config", [
    {"co2_cap_t": 1e9},                       # a cap far above the ceiling
    {"re_share_min": 0.1},                    # an easily-met floor
    {"gas_price": 25.0, "coal_price": 10.0},  # ordinary prices
])
def test_preflight_notes_stay_quiet_about_sane_configs(config):
    """A warning that fires on everything is noise. Only the shedding note —
    which states the study's premise rather than flagging a problem — is
    allowed on a config with nothing wrong with it."""
    from adapters.pypsa_toy import runner
    from adapters.pypsa_toy.network import horizon_summary

    cfg = schema.apply_defaults(config)
    notes = runner.preflight_notes(cfg, horizon_summary(cfg))
    assert not [n for n in notes if n.startswith("WARNING")], notes


def test_preflight_notes_fire_for_the_fixtures_that_advertise_them():
    from adapters.pypsa_toy import runner
    from adapters.pypsa_toy.network import horizon_summary

    for name, fragment in [("tierC_co2_cap_zero_no_shedding", "co2_cap_t"),
                           ("tierC_re_floor_impossible", "re_share_min"),
                           ("output_anomaly_free_gas", "gas_price")]:
        cfg = schema.apply_defaults(
            json.loads((FIXTURES / name / "config.json").read_text()))
        notes = runner.preflight_notes(cfg, horizon_summary(cfg))
        assert any(n.startswith("WARNING") and fragment in n for n in notes), (name, notes)


def test_missing_pypsa_is_not_a_cacheable_error():
    """ERROR/environment must never be stored by the solve cache."""
    from eval.cache import default_cacheable
    from framework.run_record import Execution

    status, origin = _parse("ModuleNotFoundError: No module named 'pypsa'", 1)
    assert (status, origin) == ("ERROR", "environment")
    assert default_cacheable(Execution(termination_status=status, error_origin=origin)) is False
    # the config-deterministic origins are still cached
    assert default_cacheable(Execution(termination_status="ERROR", error_origin="preflight")) is True
    assert default_cacheable(Execution(termination_status="INFEASIBLE", error_origin="solver")) is True


def test_shrinking_the_horizon_under_a_carbon_cap_is_tier_c():
    """snapshots_days is Tier B, but shortening it while co2_cap_t is set makes
    the absolute cap non-binding — that transition must stop the loop."""
    from framework.interventions import ProposedChange, Tier, decide
    from adapters.pypsa_toy import PypsaToyAdapter

    spec = PypsaToyAdapter().intervention_spec()
    capped = {"co2_cap_t": 10000.0, "snapshots_days": 14, "demand_scale": 1.0}

    d = decide(ProposedChange("snapshots_days", 14, 1), spec, config=capped)
    assert d.tier is Tier.C and not d.auto_apply and d.blocked_for_human
    d = decide(ProposedChange("demand_scale", 1.0, 0.25), spec, config=capped)
    assert d.tier is Tier.C and not d.auto_apply

    # tightening directions stay Tier B ...
    assert decide(ProposedChange("snapshots_days", 1, 14), spec, config=capped).tier is Tier.B
    assert decide(ProposedChange("demand_scale", 1.0, 2.0), spec, config=capped).tier is Tier.B
    # ... and with no cap in force there is nothing to relax
    uncapped = {"co2_cap_t": None, "snapshots_days": 14}
    assert decide(ProposedChange("snapshots_days", 14, 1), spec, config=uncapped).auto_apply
