"""The guardrail is the research contribution, so it gets the strictest tests.

These run without a solver — they pin the tiered-intervention behaviour that the
refiner skill is forbidden from bypassing, against the garuda adapter's
modeler-approved tier declaration.
"""

from types import SimpleNamespace

import pytest

from framework import (
    InterventionSpec,
    ProposedChange,
    RunRecord,
    TransitionRule,
    apply_refinements,
    decide,
)
from framework.interventions import Tier
from adapters.garuda import GarudaAdapter

SPEC = GarudaAdapter().intervention_spec()


def _record():
    return RunRecord(config={
        "island": "maluku", "year": "2030", "scenario": "base",
        "clean": "clean", "CO235reduction": False,
        "BAUCO2emissions": 0.0, "CO2_limit": 5_820_000,
        "engine": "dispatch", "solver": "highs",
    })


# ---- the declaration itself ------------------------------------------------

def test_tier_declaration_is_the_approved_one():
    assert SPEC.tier_a_keys == {"mipgap", "time_limit", "solver", "lp_method", "run_tag"}
    assert SPEC.tier_b_keys == {"scenario", "engine", "relax_uc", "exact_connect",
                                "import_price", "export_price", "village_storage_max_mwh",
                                "battery_duration_h"}
    assert SPEC.tier_c_keys == {"clean", "CO2_limit", "RE_limit", "CO235reduction",
                                "BAUCO2emissions", "policy_scope",
                                "export_backed_by_generation"}
    assert set(SPEC.allowed_values) == {"scenario", "clean", "engine", "solver",
                                        "policy_scope", "lp_method"}
    assert SPEC.allowed_values["engine"] == ["expansion", "dispatch"]
    assert SPEC.allowed_values["solver"] == ["highs", "gurobi"]
    assert SPEC.allowed_values["policy_scope"] == ["grid", "system"]
    assert SPEC.allowed_values["lp_method"] == list(range(-1, 6))
    assert set(SPEC.allowed_values["scenario"]) == {
        "base", "grid", "village", "gridvillage", "highimportprice", "nocoal",
        "captive", "gridcaptive"}
    assert SPEC.allowed_values["clean"] == ["reference", "clean"]
    # no key sits in two tiers
    assert not (SPEC.tier_a_keys & SPEC.tier_b_keys)
    assert not (SPEC.tier_b_keys & SPEC.tier_c_keys)
    assert not (SPEC.tier_a_keys & SPEC.tier_c_keys)


# ---- classification --------------------------------------------------------

@pytest.mark.parametrize("key,after,tier,auto", [
    ("mipgap", 0.05, "A", True),                 # numeric → auto
    ("time_limit", 3600.0, "A", True),
    ("solver", "gurobi", "A", True),             # within enum → auto
    ("lp_method", 2, "A", True),
    ("run_tag", "sweep1", "A", True),
    ("import_price", 80.0, "B", True),           # sanctioned param → auto + flag
    ("export_price", 0.0, "B", True),
    ("village_storage_max_mwh", 400.0, "B", True),
    ("battery_duration_h", 4.0, "B", True),
    ("scenario", "nocoal", "B", True),           # within enum → auto
    ("engine", "expansion", "B", True),
    ("relax_uc", True, "B", True),
    ("exact_connect", True, "B", True),
    ("CO2_limit", 9_000_000, "C", False),        # policy → blocked
    ("RE_limit", 0.1, "C", False),               # policy → blocked
    ("clean", "reference", "C", False),          # policy lever → blocked
    ("CO235reduction", False, "C", False),
    ("BAUCO2emissions", 1.0, "C", False),
    ("policy_scope", "system", "C", False),
    ("export_backed_by_generation", False, "C", False),
    ("island", "sumatera", "C", False),          # dataset choice: no tier → C
    ("year", "2035", "C", False),
    ("made_up_key", 1, "C", False),              # unknown → defaults to C
])
def test_classification(key, after, tier, auto):
    d = decide(ProposedChange(key, None, after), SPEC)
    assert d.tier.value == tier
    assert d.auto_apply is auto
    assert d.rejected is False


@pytest.mark.parametrize("key,after", [
    ("scenario", "frobnicate"),
    ("solver", "cplex"),
    ("engine", "simulation"),
    ("policy_scope", "national"),
    ("lp_method", 7),
    ("clean", "dirty"),
])
def test_illegal_enum_value_is_rejected(key, after):
    d = decide(ProposedChange(key, None, after), SPEC)
    assert d.rejected is True
    assert d.auto_apply is False


# ---- the critical negative test: Tier C is never auto-applied ---------------

def test_tier_c_policy_relaxation_is_blocked_not_applied():
    rec = _record()
    before = rec.config["CO2_limit"]
    outcome = apply_refinements(
        rec,
        [ProposedChange("CO2_limit", before, before * 10)],  # relax the cap
        SPEC,
    )
    # Config is UNCHANGED — the cap was not silently loosened.
    assert outcome.next_config["CO2_limit"] == before
    assert outcome.needs_human is True
    assert len(outcome.blocked) == 1
    assert outcome.blocked[0].applied is False
    # And it is recorded in the audit trail.
    assert rec.refinement_history[-1].tier == "C"
    assert rec.refinement_history[-1].applied is False


def test_dropping_the_clean_run_is_also_blocked():
    # 'clean' -> 'reference' switches the policy constraints off: same harm as
    # relaxing the cap, so it must halt for a human even though it is in-enum.
    rec = _record()
    outcome = apply_refinements(rec, [ProposedChange("clean", "clean", "reference")], SPEC)
    assert outcome.next_config["clean"] == "clean"
    assert outcome.needs_human
    assert not outcome.applied and not outcome.rejected


def test_tier_a_is_applied_to_next_config():
    rec = _record()
    outcome = apply_refinements(rec, [ProposedChange("mipgap", None, 0.05)], SPEC)
    assert outcome.next_config["mipgap"] == 0.05
    assert not outcome.needs_human
    assert outcome.applied[0].applied is True
    # original record config is not mutated
    assert "mipgap" not in rec.config


def test_tier_b_is_applied_and_flagged_in_audit_trail():
    rec = _record()
    outcome = apply_refinements(rec, [ProposedChange("engine", "dispatch", "expansion")], SPEC)
    assert outcome.next_config["engine"] == "expansion"
    assert not outcome.needs_human
    assert outcome.applied[0].tier == "B"
    assert rec.refinement_history[-1].tier == "B"
    assert rec.refinement_history[-1].applied is True


def test_mixed_batch_applies_safe_blocks_policy():
    rec = _record()
    outcome = apply_refinements(rec, [
        ProposedChange("time_limit", None, 600.0),    # A → applied
        ProposedChange("import_price", None, 90.0),   # B → applied
        ProposedChange("CO2_limit", None, 9e9),       # C → blocked
        ProposedChange("solver", "highs", "cplex"),   # illegal → rejected
    ], SPEC)
    assert outcome.next_config["time_limit"] == 600.0
    assert outcome.next_config["import_price"] == 90.0
    assert outcome.next_config["CO2_limit"] == rec.config["CO2_limit"]  # untouched
    assert outcome.next_config["solver"] == "highs"                     # untouched
    assert len(outcome.applied) == 2
    assert len(outcome.blocked) == 1
    assert len(outcome.rejected) == 1
    assert outcome.needs_human
    assert len(rec.refinement_history) == 4


# ---- contract roundtrip ----------------------------------------------------

def test_run_record_roundtrips_with_history(tmp_path):
    rec = _record()
    apply_refinements(rec, [ProposedChange("mipgap", None, 0.05),
                            ProposedChange("CO2_limit", None, 9e9)], SPEC)
    p = rec.save(tmp_path / "run_record.json")
    loaded = RunRecord.load(p)
    assert loaded.config_hash == rec.config_hash
    assert loaded.refinement_history[0].change == {"mipgap": [None, 0.05]}
    assert loaded.refinement_history[1].tier == "C"
    assert loaded.refinement_history[1].applied is False


# ---- value-level escalations (a Tier B key, a Tier C transition) -----------

def test_engine_switch_is_tier_b_in_both_directions():
    """Both garuda engines share build_model! — the same non-served-energy slack
    (vNSE/vVIL_NSE, optimizer.jl) and the same CO2 cap / RE floor (dispatch_engine.jl
    passes them through unchanged). Switching engine fixes capacity and relaxes
    unit commitment; it relaxes neither demand nor a policy constraint, so the
    modeler-approved Tier B holds in both directions and no transition rule
    escalates it."""
    for before, after in (("expansion", "dispatch"), ("dispatch", "expansion")):
        d = decide(ProposedChange("engine", before, after), SPEC)
        assert d.tier is Tier.B and d.auto_apply and not d.rejected
    assert not any(r.key == "engine" for r in SPEC.transition_rules)


def test_leaving_a_nocoal_scenario_is_tier_c():
    d = decide(ProposedChange("scenario", "nocoal", "grid"), SPEC)
    assert d.tier is Tier.C and not d.auto_apply
    d = decide(ProposedChange("scenario", "highimportprice", "gridvillage"), SPEC)
    assert d.tier is Tier.C and not d.auto_apply
    # entering the restriction, and unrelated scenario moves, stay Tier B
    assert decide(ProposedChange("scenario", "grid", "nocoal"), SPEC).tier is Tier.B
    assert decide(ProposedChange("scenario", "base", "grid"), SPEC).tier is Tier.B


def test_an_illegal_scenario_value_is_still_rejected_before_any_escalation():
    d = decide(ProposedChange("scenario", "nocoal", "banana"), SPEC)
    assert d.rejected and not d.auto_apply


def test_apply_refinements_blocks_an_escalated_transition():
    rec = _record()
    rec.config["scenario"] = "nocoal"
    out = apply_refinements(
        rec, [SimpleNamespace(key="scenario", before="nocoal", after="grid",
                              rationale="make it feasible")],
        SPEC)
    assert out.needs_human and not out.applied
    assert out.next_config["scenario"] == "nocoal"
    assert out.blocked[0].tier == "C"


def test_a_transition_rule_can_never_downgrade_a_policy_key():
    """Rules may only make a change stricter — an adapter cannot use one to turn
    a Tier C key into an auto-applied one."""
    spec = InterventionSpec(
        tier_c_keys={"co2_cap"},
        transition_rules=[TransitionRule(key="co2_cap", tier=Tier.A,
                                         reason="tries to downgrade")],
    )
    d = decide(ProposedChange("co2_cap", 100, 1_000_000), spec)
    assert d.tier is Tier.C and not d.auto_apply


def test_cross_key_rules_do_not_fire_without_a_config():
    """A rule conditioned on another key cannot claim its condition holds when
    the caller gave no context."""
    spec = InterventionSpec(
        tier_b_keys={"horizon"},
        transition_rules=[TransitionRule(key="horizon", tier=Tier.C, direction="decrease",
                                         when_config_keys=["cap"], reason="shrinks the cap")],
    )
    assert decide(ProposedChange("horizon", 14, 1), spec).auto_apply
    assert not decide(ProposedChange("horizon", 14, 1), spec, config={"cap": 1.0}).auto_apply
