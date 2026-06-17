"""Fixture-level checks that need no solver.

The infeasibility fixture requires a live solve (see scripts/demo_refine_loop.py);
the validation-catchable fixtures are checked here, fast.
"""

import json
from pathlib import Path

from adapters.village import VillageAdapter

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "fixtures"
ADAPTER = VillageAdapter()


def _cfg(name):
    return json.loads((FIXTURES / name / "config.json").read_text())


def test_illegal_scenario_is_rejected_by_validation():
    result = ADAPTER.validate_config(_cfg("illegal_scenario"))
    assert not result.ok
    assert any("Unknown scenario" in e for e in result.errors)


def test_bad_island_caught_by_validation():
    result = ADAPTER.validate_config(_cfg("preflight_bad_island"))
    assert not result.ok
    assert any("Input data directory not found" in e for e in result.errors)


def test_infeasible_fixture_is_a_valid_config():
    # It must pass validation so it actually reaches the solver — its breakage
    # is genuine infeasibility, not a malformed config.
    result = ADAPTER.validate_config(_cfg("infeasible_negative_co2"))
    assert result.ok, result.errors
