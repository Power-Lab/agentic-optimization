"""Supervisor loop tests via a mock adapter — no solver needed.

The mock returns TIME_LIMIT until ``mipgap`` reaches a threshold, then OPTIMAL.
That lets us pin the loop's behaviour deterministically: convergence on a Tier-A
fix, the Tier-C halt, cycle detection, exhaustion, and the stuck case. Driving a
real model through the same sequence would cost a solve per iteration, so this is
where that guarantee is locked in.
"""

from pathlib import Path

from framework import (
    Execution,
    InterventionSpec,
    ProposedChange,
    StopCriteria,
    Supervisor,
    ValidationResult,
)
from framework.adapter import Adapter


class MockAdapter(Adapter):
    name = "mock"

    def __init__(self, solve_at_mipgap=0.05):
        self.solve_at = solve_at_mipgap

    def validate_config(self, config):
        return ValidationResult(ok=True)

    def run(self, config, run_dir):
        run_dir = Path(run_dir)
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "solver.log").write_text("mock run\n")
        status = "OPTIMAL" if config.get("mipgap", 0.0) >= self.solve_at else "TIME_LIMIT"
        return Execution(termination_status=status, wall_seconds=0.0,
                         solver_log="solver.log", returncode=0)

    def intervention_spec(self):
        return InterventionSpec(
            tier_a_keys={"mipgap"},
            tier_c_keys={"CO2_limit"},
            allowed_values={},
        )

    def locate_outputs(self, run_dir):
        return {}


def raise_mipgap(_record):
    return [ProposedChange("mipgap", None, 0.05)]


def test_converges_on_tier_a_fix(tmp_path):
    sup = Supervisor(MockAdapter(solve_at_mipgap=0.05))
    result = sup.run({}, raise_mipgap, tmp_path, StopCriteria(max_iters=5))
    assert result.solved
    assert result.outcome == "solved"
    assert result.iterations == 2  # TIME_LIMIT, then OPTIMAL


def test_tier_c_proposal_halts_for_human(tmp_path):
    sup = Supervisor(MockAdapter(solve_at_mipgap=99))  # never solves numerically
    result = sup.run({}, lambda r: [ProposedChange("CO2_limit", None, 9e9)],
                     tmp_path, StopCriteria(max_iters=5))
    assert result.outcome == "needs_human"
    assert result.iterations == 1  # stopped immediately, did not loop


def test_cycle_detection(tmp_path):
    # Always proposes the same Tier-A value -> the refined config repeats.
    sup = Supervisor(MockAdapter(solve_at_mipgap=99))
    result = sup.run({}, raise_mipgap, tmp_path, StopCriteria(max_iters=9))
    assert result.outcome == "cycle"


def test_exhausts_max_iters(tmp_path):
    # Each step makes a *new* config but never solves.
    sup = Supervisor(MockAdapter(solve_at_mipgap=99))
    bump = lambda r: [ProposedChange("mipgap", None, round(r.config.get("mipgap", 0.0) + 0.01, 3))]
    result = sup.run({}, bump, tmp_path, StopCriteria(max_iters=3))
    assert result.outcome == "exhausted"
    assert result.iterations == 3


def test_stuck_when_no_proposal(tmp_path):
    sup = Supervisor(MockAdapter(solve_at_mipgap=99))
    result = sup.run({}, lambda r: [], tmp_path, StopCriteria(max_iters=5))
    assert result.outcome == "stuck"
    assert result.iterations == 1
