"""The guardrail is the research contribution, so it gets the strictest tests.

These run without a solver — they pin the tiered-intervention behaviour that the
refiner skill is forbidden from bypassing.
"""

import pytest

from framework import (
    ProposedChange,
    RunRecord,
    apply_refinements,
    decide,
)
from adapters.village import VillageAdapter

SPEC = VillageAdapter().intervention_spec()


def _record():
    return RunRecord(config={
        "island": "maluku", "year": "2030", "scenario": "base",
        "clean": "reference", "CO235reduction": False,
        "BAUCO2emissions": 0.0, "CO2_limit": 5820000,
    })


# ---- classification --------------------------------------------------------

@pytest.mark.parametrize("key,after,tier,auto", [
    ("mipgap", 0.05, "A", True),                 # numeric → auto
    ("import_price", 80.0, "B", True),           # sanctioned param → auto
    ("village_storage_max_mwh", 400.0, "B", True),
    ("scenario", "nocoal", "B", True),           # within enum → auto
    ("CO2_limit", 9_000_000, "C", False),        # policy → blocked
    ("RE_limit", 0.1, "C", False),               # policy → blocked
    ("clean", "clean", "C", False),              # policy lever → blocked
    ("made_up_key", 1, "C", False),              # unknown → defaults to C
])
def test_classification(key, after, tier, auto):
    d = decide(ProposedChange(key, None, after), SPEC)
    assert d.tier.value == tier
    assert d.auto_apply is auto


def test_illegal_enum_value_is_rejected():
    d = decide(ProposedChange("scenario", "base", "frobnicate"), SPEC)
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


def test_tier_a_is_applied_to_next_config():
    rec = _record()
    outcome = apply_refinements(rec, [ProposedChange("mipgap", None, 0.05)], SPEC)
    assert outcome.next_config["mipgap"] == 0.05
    assert not outcome.needs_human
    assert outcome.applied[0].applied is True
    # original record config is not mutated
    assert "mipgap" not in rec.config


def test_mixed_batch_applies_safe_blocks_policy():
    rec = _record()
    outcome = apply_refinements(rec, [
        ProposedChange("mipgap", None, 0.05),         # A → applied
        ProposedChange("import_price", None, 90.0),   # B → applied
        ProposedChange("CO2_limit", None, 9e9),       # C → blocked
    ], SPEC)
    assert outcome.next_config["mipgap"] == 0.05
    assert outcome.next_config["import_price"] == 90.0
    assert outcome.next_config["CO2_limit"] == rec.config["CO2_limit"]  # untouched
    assert len(outcome.applied) == 2
    assert len(outcome.blocked) == 1
    assert outcome.needs_human


# ---- contract roundtrip ----------------------------------------------------

def test_run_record_roundtrips_with_history(tmp_path):
    rec = _record()
    apply_refinements(rec, [ProposedChange("mipgap", None, 0.05)], SPEC)
    p = rec.save(tmp_path / "run_record.json")
    loaded = RunRecord.load(p)
    assert loaded.config_hash == rec.config_hash
    assert loaded.refinement_history[0].change == {"mipgap": [None, 0.05]}
