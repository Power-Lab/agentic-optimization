"""Headless driver + provider seam tests — no model, no solver, no real CLI.

Two layers are covered here:

1. :mod:`framework.llm` — the provider seam. :class:`FakeLLMClient` is exercised
   directly, and :class:`ClaudeCLIClient` is exercised against a **stub ``claude``
   shell script** written into ``tmp_path`` and put first on ``PATH``. The stub
   records the argv, the stdin and one env var, then replays a canned JSON
   envelope. The real ``claude`` binary is never invoked: every test either
   points ``claude_bin`` at an absolute stub path or replaces ``PATH`` entirely
   with the stub directory, so a missing stub fails loudly instead of falling
   through to the real CLI.

2. :mod:`framework.agent_driver` — the roles. A :class:`FakeLLMClient` returns
   canned JSON per role and we assert the driver turns it into *typed* framework
   objects (``Diagnosis``, ``Proposal``, ``Anomaly``), writes the audit files,
   and plugs into :class:`framework.supervisor.Supervisor` through
   ``make_propose_fn``.

Everything runs on the stdlib (no pandas/pypsa), so both interpreters can run it.
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from framework import (
    Execution,
    InterventionSpec,
    StopCriteria,
    Supervisor,
    ValidationResult,
)
from framework.adapter import Adapter
from framework.agent_driver import (
    ANOMALY_SCHEMA,
    DIAGNOSIS_SCHEMA,
    PREAMBLE,
    PROPOSAL_SCHEMA,
    ROLE_SCHEMAS,
    ROLES,
    SCENARIO_SCHEMA,
    AgentDriver,
    Proposal,
    ProposalSet,
    default_skills_dir,
    make_analyze_fn,
    make_propose_fn,
    strip_front_matter,
    tail_lines,
)
from framework.interventions import ProposedChange
from framework.llm import (
    NESTING_ENV_VARS,
    ClaudeCLIClient,
    FakeLLMClient,
    LLMError,
    available_clients,
    extract_json_object,
    make_client,
    register_client,
    role_of,
)
from framework.run_record import Anomaly, Diagnosis, RunRecord

# ---------------------------------------------------------------------------
# a stub adapter: deterministic, in-process, no subprocess and no solver
# ---------------------------------------------------------------------------


class StubAdapter(Adapter):
    """Solves iff ``mipgap >= solve_at``; otherwise TIME_LIMIT. Writes a log."""

    name = "stub"

    def __init__(self, solve_at: float = 0.05, outputs: dict | None = None):
        self.solve_at = solve_at
        self.outputs = outputs or {}
        self.calls: list = []

    def validate_config(self, config):
        errors = [f"missing required key {k!r}" for k in ("island",) if k not in config]
        return ValidationResult(ok=not errors, errors=errors)

    def run(self, config, run_dir):
        run_dir = Path(run_dir)
        run_dir.mkdir(parents=True, exist_ok=True)
        self.calls.append(dict(config))
        solved = float(config.get("mipgap", 0.0)) >= self.solve_at
        status = "OPTIMAL" if solved else "TIME_LIMIT"
        (run_dir / "solver.log").write_text(
            "Running HiGHS 1.7.0\n"
            f"Reading config mipgap={config.get('mipgap')}\n"
            "Solving MIP model\n"
            f"Model status : {status}\n"
        )
        out_dir = run_dir / "outputs"
        for name, text in self.outputs.items():
            out_dir.mkdir(parents=True, exist_ok=True)
            (out_dir / f"{name}.csv").write_text(text)
        return Execution(
            termination_status=status,
            wall_seconds=0.01,
            solver_log="solver.log",
            returncode=0,
        )

    def intervention_spec(self):
        return InterventionSpec(
            tier_a_keys={"mipgap", "time_limit"},
            tier_b_keys={"scenario"},
            tier_c_keys={"CO2_limit", "clean"},
            allowed_values={"scenario": ["base", "grid"]},
        )

    def locate_outputs(self, run_dir):
        out_dir = Path(run_dir) / "outputs"
        return {p.stem: p for p in sorted(out_dir.glob("*.csv"))} if out_dir.is_dir() else {}

    def describe_config(self):
        return (
            "Stub model. Required: island. Optional: mipgap (solver gap, Tier A), "
            "time_limit (seconds, Tier A), scenario in {base, grid} (Tier B), "
            "CO2_limit (policy cap, Tier C), clean (policy flag, Tier C)."
        )


BASE_CONFIG = {"island": "maluku", "mipgap": 0.01}


def make_record(config=None, status="TIME_LIMIT", run_dir: Path | None = None) -> RunRecord:
    record = RunRecord(config=dict(config or BASE_CONFIG))
    record.execution = Execution(
        termination_status=status, wall_seconds=1.5, solver_log="solver.log", returncode=0
    )
    if run_dir is not None:
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "solver.log").write_text(
            "\n".join(f"line {i}" for i in range(1, 21)) + "\nModel status : TIME_LIMIT\n"
        )
    return record


DIAGNOSIS_REPLY = {
    "status": "TIME_LIMIT",
    "root_cause": "the MIP gap is tighter than the time budget allows",
    "evidence": ["log:21", "config:mipgap"],
    "suggested_intervention_tier": "A",
    "confidence": 0.82,
}

PROPOSAL_REPLY = {
    "proposals": [
        {
            "key": "mipgap",
            "after": 0.05,
            "rationale": "loosen the gap so the incumbent is accepted",
            "tier_claimed": "A",
        }
    ],
    "disclosed": False,
    "human_question": None,
    "summary": "one Tier-A numeric change",
}

ANOMALY_REPLY = {
    "anomalies": [
        {
            "metric": "Total_NSE_MWh",
            "value": 205997.3,
            "expected": "~0 for a reliable system",
            "severity": "HIGH",
        }
    ],
    "plausible": False,
    "summary": "unserved energy is far above tolerance",
}


# ---------------------------------------------------------------------------
# stub `claude` CLI
# ---------------------------------------------------------------------------

ARG_SEP = "\n<<<ARG>>>\n"

ENVELOPE = {
    "type": "result",
    "subtype": "success",
    "is_error": False,
    "num_turns": 1,
    "duration_ms": 1234,
    "duration_api_ms": 1100,
    "total_cost_usd": 0.0123,
    "usage": {"input_tokens": 900, "output_tokens": 120},
    "session_id": "stub-session",
    "result": json.dumps(DIAGNOSIS_REPLY),
    "structured_output": DIAGNOSIS_REPLY,
}


def write_claude_stub(bin_dir: Path, *, stdout: str = "", stderr: str = "", exit_code: int = 0,
                      name: str = "claude") -> SimpleNamespace:
    """Write an executable stub standing in for the ``claude`` CLI.

    It records how it was called (argv, stdin, whether the nesting env vars
    survived), replays ``stdout``, and exits with ``exit_code``. It never talks
    to a model.
    """
    bin_dir.mkdir(parents=True, exist_ok=True)
    stub = bin_dir / name
    payload = bin_dir / f"{name}.stdout"
    argv_file = bin_dir / f"{name}.argv"
    stdin_file = bin_dir / f"{name}.stdin"
    env_file = bin_dir / f"{name}.env"
    payload.write_text(stdout)
    q = shlex.quote
    stderr_line = f"printf '%s' {q(stderr)} >&2" if stderr else ":"
    # Each argument is written followed by the ARG_SEP marker, so arguments that
    # themselves span lines (the system prompt does) round-trip intact.
    script = "\n".join([
        "#!/bin/sh",
        "# test stub for the claude CLI: records the call, replays a canned envelope.",
        f": > {q(str(argv_file))}",
        f"""for a in "$@"; do printf '%s\\n<<<ARG>>>\\n' "$a" >> {q(str(argv_file))}; done""",
        f"if [ -t 0 ]; then : > {q(str(stdin_file))}; else cat > {q(str(stdin_file))}; fi",
        f"""printf 'CLAUDECODE=%s\\n' "${{CLAUDECODE-<unset>}}" > {q(str(env_file))}""",
        f"""printf 'CLAUDE_CODE_ENTRYPOINT=%s\\n' "${{CLAUDE_CODE_ENTRYPOINT-<unset>}}" >> {q(str(env_file))}""",
        stderr_line,
        f"cat {q(str(payload))}",
        f"exit {int(exit_code)}",
        "",
    ])
    stub.write_text(script)
    stub.chmod(0o755)
    return SimpleNamespace(
        path=stub,
        argv_file=argv_file,
        stdin_file=stdin_file,
        env_file=env_file,
        argv=lambda: [a for a in argv_file.read_text().split(ARG_SEP)[:-1]],
        stdin=lambda: stdin_file.read_text(),
        env=lambda: env_file.read_text(),
    )


@pytest.fixture
def stub_cli(tmp_path):
    """A successful stub, with PATH replaced by its directory only."""
    return write_claude_stub(tmp_path / "bin", stdout=json.dumps(ENVELOPE))


@pytest.fixture
def only_stub_on_path(monkeypatch, stub_cli):
    """PATH is replaced by the stub dir plus the system bin dirs (the stub is a
    shell script and needs ``cat``). The real ``claude`` lives in neither, and
    the assertion below makes sure of it: this test-suite never invokes it."""
    monkeypatch.setenv("PATH", os.pathsep.join([str(stub_cli.path.parent), "/usr/bin", "/bin"]))
    assert shutil.which("claude") == str(stub_cli.path)
    return stub_cli


# ---------------------------------------------------------------------------
# prompt assembly
# ---------------------------------------------------------------------------


def test_every_role_has_a_schema_and_a_skill_file():
    assert set(ROLES) == set(ROLE_SCHEMAS)
    for role in ROLES:
        assert (default_skills_dir() / role / "SKILL.md").is_file(), role
    assert SCENARIO_SCHEMA["required"] == ["config"]
    assert "proposals" in PROPOSAL_SCHEMA["required"]
    assert "anomalies" in ANOMALY_SCHEMA["required"]
    assert "suggested_intervention_tier" in DIAGNOSIS_SCHEMA["required"]


def test_system_prompt_carries_role_marker_preamble_skill_and_adapter():
    driver = AgentDriver(FakeLLMClient(), StubAdapter())
    system = driver.system_prompt("refiner")
    assert system.startswith("[role: refiner]")
    assert role_of(system) == "refiner"
    assert PREAMBLE in system
    # from the real .claude/skills/refiner/SKILL.md
    assert "Tier C" in system and "apply_refinements" in system
    # from the adapter, not hard-coded in the framework
    assert "Stub model" in system and "CO2_limit" in system


def test_system_prompt_strips_yaml_front_matter():
    driver = AgentDriver(FakeLLMClient(), StubAdapter())
    text = driver.skill_text("log-analyzer")
    assert not text.lstrip().startswith("---")
    assert "description:" not in text.splitlines()[0]
    assert strip_front_matter("---\nname: x\n---\n\n# Title\n") == "# Title\n"
    assert strip_front_matter("# No front matter\n") == "# No front matter\n"


def test_system_prompt_falls_back_when_skills_dir_is_missing(tmp_path):
    driver = AgentDriver(FakeLLMClient(), StubAdapter(), skills_dir=tmp_path / "nope")
    system = driver.system_prompt("output-analyzer")
    assert system.startswith("[role: output-analyzer]")
    assert "Output Analyzer" in system
    assert "Stub model" in system


def test_unknown_role_is_rejected():
    driver = AgentDriver(FakeLLMClient(), StubAdapter())
    with pytest.raises(ValueError):
        driver.system_prompt("not-a-role")


def test_user_prompt_has_record_numbered_log_tail_and_tiers(tmp_path):
    client = FakeLLMClient({"log-analyzer": DIAGNOSIS_REPLY})
    driver = AgentDriver(client, StubAdapter(), log_tail_lines=5)
    record = make_record(run_dir=tmp_path)
    driver.diagnose(record, run_dir=tmp_path)

    user = client.calls[0]["user"]
    assert "## Run record" in user and '"island": "maluku"' in user
    assert "## Solver log (last 5 lines, numbered)" in user
    assert "21: Model status : TIME_LIMIT" in user
    assert "## Intervention tiers" in user
    assert "C_policy_human_sign_off" in user and "CO2_limit" in user
    assert client.calls[0]["schema"] is DIAGNOSIS_SCHEMA


def test_tail_lines_numbers_and_truncates(tmp_path):
    log = tmp_path / "solver.log"
    log.write_text("\n".join(f"l{i}" for i in range(1, 11)) + "\n")
    tail = tail_lines(log, 3)
    assert tail.splitlines() == ["8: l8", "9: l9", "10: l10"]
    assert tail_lines(tmp_path / "missing.log", 3) == ""


def test_tiers_come_from_the_adapter_spec():
    driver = AgentDriver(FakeLLMClient(), StubAdapter())
    tiers = driver.tiers()
    assert tiers["A_auto_apply_numerics"] == ["mipgap", "time_limit"]
    assert tiers["B_auto_apply_flagged_parameters"] == ["scenario"]
    assert tiers["C_policy_human_sign_off"] == ["CO2_limit", "clean"]
    assert tiers["allowed_values"] == {"scenario": ["base", "grid"]}
    assert "Tier C" in tiers["unknown_keys"]


# ---------------------------------------------------------------------------
# roles -> typed objects
# ---------------------------------------------------------------------------


def test_build_scenario_returns_config_and_records_validation(tmp_path):
    reply = {"config": {"island": "maluku", "mipgap": 0.01},
             "lever_mapping": {"fast": "mipgap"}, "unexpressed": [], "notes": "ok"}
    client = FakeLLMClient({"scenario-builder": reply})
    driver = AgentDriver(client, StubAdapter())
    config = driver.build_scenario("a fast maluku dispatch case", run_dir=tmp_path)

    assert config == {"island": "maluku", "mipgap": 0.01}
    assert "a fast maluku dispatch case" in client.calls[0]["user"]
    audit = json.loads((tmp_path / "llm" / "scenario-builder_1.json").read_text())
    assert audit["validation"] == {"ok": True, "errors": []}


def test_build_scenario_reports_a_config_that_fails_validation(tmp_path):
    client = FakeLLMClient({"scenario-builder": {"config": {"mipgap": 0.01}}})
    driver = AgentDriver(client, StubAdapter())
    driver.build_scenario("no island", run_dir=tmp_path)
    audit = json.loads((tmp_path / "llm" / "scenario-builder_1.json").read_text())
    assert audit["validation"]["ok"] is False
    assert "island" in audit["validation"]["errors"][0]


def test_build_scenario_without_a_config_raises(tmp_path):
    client = FakeLLMClient({"scenario-builder": {"notes": "I could not decide"}})
    driver = AgentDriver(client, StubAdapter())
    with pytest.raises(LLMError, match="config"):
        driver.build_scenario("something", run_dir=tmp_path)
    assert (tmp_path / "llm" / "scenario-builder_1.json").is_file()


def test_diagnose_returns_a_typed_diagnosis_and_writes_it_into_the_record(tmp_path):
    client = FakeLLMClient({"log-analyzer": DIAGNOSIS_REPLY})
    driver = AgentDriver(client, StubAdapter())
    record = make_record(run_dir=tmp_path)

    diagnosis = driver.diagnose(record, run_dir=tmp_path)

    assert isinstance(diagnosis, Diagnosis)
    assert diagnosis.status == "TIME_LIMIT"
    assert diagnosis.suggested_intervention_tier == "A"
    assert diagnosis.evidence == ["log:21", "config:mipgap"]
    assert diagnosis.confidence == pytest.approx(0.82)
    assert record.log_diagnosis is diagnosis
    # survives the round trip through run_record.json
    record.save(tmp_path / "run_record.json")
    assert RunRecord.load(tmp_path / "run_record.json").log_diagnosis.root_cause


def test_diagnose_coerces_sloppy_model_output(tmp_path):
    sloppy = {
        "status": "infeasible",
        "root_cause": "cap too tight",
        "evidence": "log:412",           # a bare string, not a list
        "suggested_intervention_tier": "c",  # lower case
        "confidence": 1.7,               # out of range
    }
    client = FakeLLMClient({"log-analyzer": sloppy})
    driver = AgentDriver(client, StubAdapter())
    diagnosis = driver.diagnose(make_record(status="INFEASIBLE"), log_tail="", run_dir=tmp_path)
    assert diagnosis.evidence == ["log:412"]
    assert diagnosis.suggested_intervention_tier == "C"
    assert diagnosis.confidence == 1.0


def test_diagnose_with_an_unusable_tier_falls_back_to_none(tmp_path):
    reply = dict(DIAGNOSIS_REPLY, suggested_intervention_tier="Tier-A-ish", confidence="high")
    driver = AgentDriver(FakeLLMClient({"log-analyzer": reply}), StubAdapter())
    diagnosis = driver.diagnose(make_record(), log_tail="", run_dir=tmp_path)
    assert diagnosis.suggested_intervention_tier is None
    assert diagnosis.confidence is None


def test_propose_returns_proposals_usable_by_the_guardrail(tmp_path):
    client = FakeLLMClient({"refiner": PROPOSAL_REPLY})
    driver = AgentDriver(client, StubAdapter())
    record = make_record(run_dir=tmp_path)

    proposals = driver.propose(record, run_dir=tmp_path)

    assert isinstance(proposals, ProposalSet) and len(proposals) == 1
    p = proposals[0]
    assert isinstance(p, Proposal) and isinstance(p, ProposedChange)
    assert (p.key, p.after) == ("mipgap", 0.05)
    assert p.before == 0.01                       # filled in from the record's config
    assert p.rationale.startswith("loosen the gap")
    assert p.disclosed is False and p.tier_claimed == "A"
    assert proposals.disclosed is False
    assert proposals.summary == "one Tier-A numeric change"
    assert proposals.human_question is None
    assert proposals.raw == PROPOSAL_REPLY


def test_propose_marks_a_policy_batch_as_disclosed_and_keeps_the_question(tmp_path):
    reply = {
        "proposals": [
            {"key": "CO2_limit", "after": 1e12, "rationale": "the cap is unreachable"},
            {"key": "mipgap", "after": 0.1, "rationale": "speed", "disclosed": False},
        ],
        "disclosed": False,
        "human_question": "Raising the CO2 cap changes the study's meaning. Approve?",
        "summary": "one policy relaxation plus a numeric",
    }
    driver = AgentDriver(FakeLLMClient({"refiner": reply}), StubAdapter())
    proposals = driver.propose(make_record(), log_tail="", run_dir=tmp_path)

    # a human_question counts as disclosure even when the flag says otherwise
    assert proposals.disclosed is True
    assert proposals.human_question.startswith("Raising the CO2 cap")
    assert proposals[0].disclosed is True          # inherits the batch flag
    assert proposals[1].disclosed is False         # explicit per-proposal override


def test_propose_drops_malformed_entries_but_records_them(tmp_path):
    reply = {
        "proposals": [
            {"after": 3},                       # no key
            "raise the time limit",             # not an object
            {"key": "time_limit", "after": 600, "rationale": "more budget"},
        ],
        "disclosed": False,
        "summary": "mixed",
    }
    driver = AgentDriver(FakeLLMClient({"refiner": reply}), StubAdapter())
    proposals = driver.propose(make_record(), log_tail="", run_dir=tmp_path)
    assert [p.key for p in proposals] == ["time_limit"]
    audit = json.loads((tmp_path / "llm" / "refiner_1.json").read_text())
    assert len(audit["dropped_proposals"]) == 2
    assert audit["parsed_proposals"][0]["key"] == "time_limit"


def test_analyze_outputs_returns_anomalies_and_appends_them(tmp_path):
    client = FakeLLMClient({"output-analyzer": ANOMALY_REPLY})
    driver = AgentDriver(client, StubAdapter(), max_rows_per_output=2)
    record = make_record(status="OPTIMAL")
    outputs = {
        "reliability_results": [{"site": "a", "Total_NSE_MWh": "12"},
                                {"site": "b", "Total_NSE_MWh": "13"},
                                {"site": "c", "Total_NSE_MWh": "14"}],
    }

    anomalies = driver.analyze_outputs(record, outputs, run_dir=tmp_path)

    assert len(anomalies) == 1 and isinstance(anomalies[0], Anomaly)
    assert anomalies[0].metric == "Total_NSE_MWh"
    assert anomalies[0].severity == "high"          # normalised from "HIGH"
    assert record.output_anomalies == anomalies
    user = client.calls[0]["user"]
    assert '"n_rows": 3' in user
    assert '"site": "c"' not in user                # head truncated to 2 rows
    assert "## Solver log" not in user              # no log tail for this role


def test_analyze_outputs_can_include_a_baseline_and_normalises_severity(tmp_path):
    reply = {"anomalies": [{"metric": "cost", "value": 1, "expected": "~2", "severity": "weird"},
                           {"value": 3}],
             "plausible": True, "summary": "s"}
    client = FakeLLMClient({"output-analyzer": reply})
    driver = AgentDriver(client, StubAdapter())
    baseline = make_record(config={"island": "maluku", "mipgap": 0.5}, status="OPTIMAL")
    anomalies = driver.analyze_outputs(make_record(status="OPTIMAL"), {}, run_dir=tmp_path,
                                       baseline=baseline)
    assert [a.severity for a in anomalies] == ["medium"]   # unknown severity, metric-less dropped
    assert "## Baseline run record" in client.calls[0]["user"]


# ---------------------------------------------------------------------------
# audit trail
# ---------------------------------------------------------------------------


def test_every_call_is_audited_under_the_run_dir(tmp_path):
    client = FakeLLMClient({"log-analyzer": [DIAGNOSIS_REPLY, DIAGNOSIS_REPLY],
                            "refiner": PROPOSAL_REPLY})
    driver = AgentDriver(client, StubAdapter())
    record = make_record(run_dir=tmp_path)
    driver.diagnose(record, run_dir=tmp_path)
    driver.propose(record, run_dir=tmp_path)
    driver.diagnose(record, run_dir=tmp_path)

    names = sorted(p.name for p in (tmp_path / "llm").glob("*.json"))
    assert names == ["log-analyzer_1.json", "log-analyzer_2.json", "refiner_1.json"]
    entry = json.loads((tmp_path / "llm" / "log-analyzer_1.json").read_text())
    assert entry["role"] == "log-analyzer" and entry["n"] == 1
    assert entry["client"] == "fake"
    assert entry["system"].startswith("[role: log-analyzer]")
    assert entry["response"] == DIAGNOSIS_REPLY
    assert entry["schema"] == DIAGNOSIS_SCHEMA
    assert entry["error"] is None
    assert isinstance(entry["elapsed_s"], float)
    assert len(driver.transcript) == 3


def test_audit_falls_back_to_log_dir_when_there_is_no_run_dir(tmp_path):
    driver = AgentDriver(FakeLLMClient({"log-analyzer": DIAGNOSIS_REPLY}), StubAdapter(),
                         log_dir=tmp_path / "audit")
    driver.diagnose(make_record(), log_tail="")
    assert (tmp_path / "audit" / "log-analyzer_1.json").is_file()


def test_provider_failure_is_audited_then_raised(tmp_path):
    driver = AgentDriver(FakeLLMClient([]), StubAdapter())   # no canned replies
    with pytest.raises(LLMError):
        driver.diagnose(make_record(), log_tail="", run_dir=tmp_path)
    entry = json.loads((tmp_path / "llm" / "log-analyzer_1.json").read_text())
    assert entry["response"] is None
    assert "exhausted" in entry["error"]["message"]


def test_a_non_object_reply_is_an_llm_error(tmp_path):
    driver = AgentDriver(FakeLLMClient(["not json at all"]), StubAdapter())
    with pytest.raises(LLMError, match="JSON object"):
        driver.diagnose(make_record(), log_tail="", run_dir=tmp_path)
    entry = json.loads((tmp_path / "llm" / "log-analyzer_1.json").read_text())
    assert entry["error"]["message"].startswith("expected a JSON object")


# ---------------------------------------------------------------------------
# supervisor glue
# ---------------------------------------------------------------------------


def test_make_propose_fn_drives_the_supervisor_to_a_solve(tmp_path):
    client = FakeLLMClient({"log-analyzer": DIAGNOSIS_REPLY, "refiner": PROPOSAL_REPLY})
    adapter = StubAdapter(solve_at=0.05)
    driver = AgentDriver(client, adapter)
    sup = Supervisor(adapter)

    result = sup.run(dict(BASE_CONFIG), make_propose_fn(driver), tmp_path,
                     StopCriteria(max_iters=4))

    assert result.outcome == "solved"
    assert result.iterations == 2
    assert adapter.calls[1]["mipgap"] == 0.05
    # the driver saw each iteration's own run dir
    iter0 = sorted(tmp_path.glob("iter00_*"))[0]
    assert (iter0 / "llm" / "log-analyzer_1.json").is_file()
    assert (iter0 / "llm" / "refiner_1.json").is_file()
    saved = RunRecord.load(iter0 / "run_record.json")
    assert saved.log_diagnosis.suggested_intervention_tier == "A"
    assert saved.refinement_history[0].tier == "A"
    assert saved.refinement_history[0].applied is True
    assert "loosen the gap" in saved.refinement_history[0].rationale


def test_make_propose_fn_can_skip_the_diagnosis_call(tmp_path):
    client = FakeLLMClient({"refiner": PROPOSAL_REPLY})
    adapter = StubAdapter(solve_at=0.05)
    driver = AgentDriver(client, adapter)
    result = Supervisor(adapter).run(dict(BASE_CONFIG),
                                     make_propose_fn(driver, diagnose=False),
                                     tmp_path, StopCriteria(max_iters=3))
    assert result.outcome == "solved"
    assert [c["role"] for c in client.calls] == ["refiner"]


def test_make_analyze_fn_reads_outputs_through_the_adapter(tmp_path):
    adapter = StubAdapter(solve_at=0.0, outputs={"cost_results": "component,value\ntotal,418.75\n"})
    client = FakeLLMClient({"output-analyzer": ANOMALY_REPLY})
    driver = AgentDriver(client, adapter)
    result = Supervisor(adapter).run(dict(BASE_CONFIG), lambda r: [], tmp_path,
                                     StopCriteria(max_iters=2), analyze_fn=make_analyze_fn(driver))

    assert result.outcome == "solved"
    run_dir = sorted(tmp_path.glob("iter00_*"))[0]
    saved = RunRecord.load(run_dir / "run_record.json")
    assert saved.output_anomalies[0].metric == "Total_NSE_MWh"
    assert "cost_results" in client.calls[0]["user"] and "418.75" in client.calls[0]["user"]


def test_make_analyze_fn_survives_missing_outputs(tmp_path):
    adapter = StubAdapter(solve_at=0.0)
    driver = AgentDriver(FakeLLMClient({"output-analyzer": {"anomalies": [], "plausible": True,
                                                           "summary": "clean"}}), adapter)
    run_dir = tmp_path / "run"
    record = make_record(status="OPTIMAL", run_dir=run_dir)
    assert make_analyze_fn(driver)(record, run_dir) == []


# ---------------------------------------------------------------------------
# FakeLLMClient itself
# ---------------------------------------------------------------------------


def test_fake_client_dispatch_modes():
    seq = FakeLLMClient([{"a": 1}, {"b": 2}])
    assert seq.complete("[role: refiner]", "u") == {"a": 1}
    assert seq.complete("[role: refiner]", "u") == {"b": 2}
    with pytest.raises(LLMError, match="exhausted"):
        seq.complete("[role: refiner]", "u")

    by_role = FakeLLMClient({"refiner": [{"n": 1}], "log-analyzer": {"reused": True}})
    assert by_role.complete("[role: log-analyzer]\n...", "u") == {"reused": True}
    assert by_role.complete("[role: log-analyzer]\n...", "u") == {"reused": True}
    assert by_role.complete("[role: refiner]\n...", "u") == {"n": 1}
    with pytest.raises(LLMError, match="exhausted"):
        by_role.complete("[role: refiner]\n...", "u")
    with pytest.raises(LLMError, match="no canned reply"):
        by_role.complete("[role: output-analyzer]\n...", "u")

    fn = FakeLLMClient(lambda system, user, schema: {"role": role_of(system), "schema": bool(schema)})
    assert fn.complete("[role: refiner]", "u", {"type": "object"}) == {"role": "refiner",
                                                                      "schema": True}
    assert fn.calls[0]["user"] == "u"


# ---------------------------------------------------------------------------
# ClaudeCLIClient: argv construction (no process is started here)
# ---------------------------------------------------------------------------


def test_build_argv_uses_the_verified_flags_and_never_bare():
    client = ClaudeCLIClient(model="sonnet", claude_bin="/nonexistent/claude")
    argv = client.build_argv("SYS", {"type": "object"}, None, "USER")

    assert argv[0] == "/nonexistent/claude"
    assert argv[1] == "-p"
    for flag, value in [("--output-format", "json"), ("--max-turns", "1"),
                        ("--system-prompt", "SYS"), ("--model", "sonnet")]:
        assert argv[argv.index(flag) + 1] == value
    assert "--no-session-persistence" in argv
    assert argv[argv.index("--tools") + 1] == ""          # tools disabled, empty string
    assert json.loads(argv[argv.index("--json-schema") + 1]) == {"type": "object"}
    assert "--bare" not in argv                            # never: it breaks CLI auth
    assert "USER" not in argv                              # stdin by default


def test_build_argv_optional_flags_and_prompt_via_arg():
    client = ClaudeCLIClient(model=None, claude_bin="/nonexistent/claude",
                             extra_args=("--debug",), max_turns=3, prompt_via="arg",
                             effort="high", max_budget_usd=0.25)
    argv = client.build_argv("SYS", None, "opus", "USER")
    assert "--json-schema" not in argv
    assert argv[argv.index("--model") + 1] == "opus"       # per-call override
    assert argv[argv.index("--effort") + 1] == "high"
    assert argv[argv.index("--max-budget-usd") + 1] == "0.25"
    assert argv[argv.index("--max-turns") + 1] == "3"
    assert argv[-2:] == ["--debug", "USER"]

    no_model = ClaudeCLIClient(model=None, claude_bin="/nonexistent/claude").build_argv("S")
    assert "--model" not in no_model

    with pytest.raises(ValueError):
        ClaudeCLIClient(prompt_via="carrier-pigeon")


# ---------------------------------------------------------------------------
# ClaudeCLIClient against the stub executable
# ---------------------------------------------------------------------------


def test_cli_client_calls_the_stub_and_returns_structured_output(only_stub_on_path, monkeypatch):
    monkeypatch.setenv("CLAUDECODE", "1")
    monkeypatch.setenv("CLAUDE_CODE_ENTRYPOINT", "cli")
    client = ClaudeCLIClient(model="sonnet", timeout=30)   # resolved from PATH

    reply = client.complete("SYSTEM PROMPT", "USER PROMPT", DIAGNOSIS_SCHEMA)

    assert reply == DIAGNOSIS_REPLY
    argv = only_stub_on_path.argv()
    assert argv[0] == "-p"
    assert argv[argv.index("--system-prompt") + 1] == "SYSTEM PROMPT"
    assert json.loads(argv[argv.index("--json-schema") + 1]) == DIAGNOSIS_SCHEMA
    assert "--bare" not in argv
    assert only_stub_on_path.stdin() == "USER PROMPT"       # prompt goes on stdin
    # the nesting guards were dropped so a nested CLI can start
    assert "CLAUDECODE=<unset>" in only_stub_on_path.env()
    assert "CLAUDE_CODE_ENTRYPOINT=<unset>" in only_stub_on_path.env()
    assert set(NESTING_ENV_VARS) == {"CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT"}
    assert client.last_meta["total_cost_usd"] == pytest.approx(0.0123)
    assert client.last_meta["usage"]["input_tokens"] == 900


def test_cli_client_can_pass_the_prompt_as_an_argument(stub_cli):
    client = ClaudeCLIClient(claude_bin=str(stub_cli.path), prompt_via="arg", timeout=30)
    client.complete("SYS", "USER PROMPT", DIAGNOSIS_SCHEMA)
    assert stub_cli.argv()[-1] == "USER PROMPT"
    assert stub_cli.stdin() == ""


def test_cli_client_without_a_schema_returns_text(tmp_path):
    envelope = dict(ENVELOPE, result="just some prose")
    envelope.pop("structured_output")
    stub = write_claude_stub(tmp_path / "bin", stdout=json.dumps(envelope))
    client = ClaudeCLIClient(claude_bin=str(stub.path), timeout=30)
    assert client.complete("SYS", "USER") == "just some prose"
    assert "--json-schema" not in stub.argv()


def test_cli_client_recovers_json_from_a_fenced_result(tmp_path):
    envelope = {"type": "result", "is_error": False,
                "result": "Here you go:\n```json\n" + json.dumps(PROPOSAL_REPLY) + "\n```\n"}
    stub = write_claude_stub(tmp_path / "bin", stdout=json.dumps(envelope))
    client = ClaudeCLIClient(claude_bin=str(stub.path), timeout=30)
    assert client.complete("SYS", "USER", PROPOSAL_SCHEMA) == PROPOSAL_REPLY


def test_cli_client_raises_on_nonzero_exit(tmp_path):
    stub = write_claude_stub(tmp_path / "bin", stdout="", stderr="not logged in\n", exit_code=1)
    client = ClaudeCLIClient(claude_bin=str(stub.path), timeout=30)
    with pytest.raises(LLMError) as excinfo:
        client.complete("SYS", "USER", DIAGNOSIS_SCHEMA)
    assert excinfo.value.returncode == 1
    assert "not logged in" in excinfo.value.stderr
    assert "not logged in" in str(excinfo.value)


def test_cli_client_raises_on_an_error_envelope(tmp_path):
    stub = write_claude_stub(
        tmp_path / "bin",
        stdout=json.dumps({"type": "result", "is_error": True, "result": "credit balance too low"}),
    )
    client = ClaudeCLIClient(claude_bin=str(stub.path), timeout=30)
    with pytest.raises(LLMError, match="credit balance too low"):
        client.complete("SYS", "USER", DIAGNOSIS_SCHEMA)


def test_cli_client_raises_on_unparseable_stdout(tmp_path):
    stub = write_claude_stub(tmp_path / "bin", stdout="segmentation fault\n")
    client = ClaudeCLIClient(claude_bin=str(stub.path), timeout=30)
    with pytest.raises(LLMError, match="no JSON envelope"):
        client.complete("SYS", "USER", DIAGNOSIS_SCHEMA)


def test_cli_client_raises_when_the_reply_has_no_json_object(tmp_path):
    stub = write_claude_stub(
        tmp_path / "bin",
        stdout=json.dumps({"type": "result", "is_error": False, "result": "sorry, no."}),
    )
    client = ClaudeCLIClient(claude_bin=str(stub.path), timeout=30)
    with pytest.raises(LLMError, match="structured_output"):
        client.complete("SYS", "USER", DIAGNOSIS_SCHEMA)


def test_cli_client_reports_a_missing_binary(tmp_path):
    client = ClaudeCLIClient(claude_bin=str(tmp_path / "definitely-not-here"))
    with pytest.raises(LLMError, match="not found"):
        client.complete("SYS", "USER")


def test_driver_runs_end_to_end_over_the_cli_stub(tmp_path, only_stub_on_path):
    """The whole seam: AgentDriver -> ClaudeCLIClient -> stub executable."""
    driver = AgentDriver(ClaudeCLIClient(timeout=30), StubAdapter())
    record = make_record(run_dir=tmp_path)
    diagnosis = driver.diagnose(record, run_dir=tmp_path)

    assert diagnosis.root_cause == DIAGNOSIS_REPLY["root_cause"]
    argv = only_stub_on_path.argv()
    system = argv[argv.index("--system-prompt") + 1]
    assert system.startswith("[role: log-analyzer]") and "Stub model" in system
    assert "## Run record" in only_stub_on_path.stdin()
    entry = json.loads((tmp_path / "llm" / "log-analyzer_1.json").read_text())
    assert entry["client"] == "claude-cli"
    assert entry["meta"]["total_cost_usd"] == pytest.approx(0.0123)


# ---------------------------------------------------------------------------
# envelope / JSON helpers
# ---------------------------------------------------------------------------


def test_parse_envelope_accepts_a_stream_json_list():
    stdout = json.dumps([{"type": "system"}, {"type": "result", "result": "ok"}])
    assert ClaudeCLIClient.parse_envelope(stdout)["result"] == "ok"


def test_parse_envelope_finds_an_envelope_in_noisy_stdout():
    stdout = 'warning: something\n{"type":"result","result":"ok"}\n'
    assert ClaudeCLIClient.parse_envelope(stdout)["result"] == "ok"
    with pytest.raises(LLMError):
        ClaudeCLIClient.parse_envelope('"just a string"')


def test_extract_json_object_variants():
    assert extract_json_object('{"a": 1}') == {"a": 1}
    assert extract_json_object('```json\n{"a": 2}\n```') == {"a": 2}
    assert extract_json_object('sure thing: {"a": 3} — hope that helps') == {"a": 3}
    assert extract_json_object("[1, 2, 3]") is None
    assert extract_json_object("") is None
    assert extract_json_object(None) is None


def test_role_of():
    assert role_of("[role: output-analyzer]\nrest") == "output-analyzer"
    assert role_of("no marker") is None


# ---------------------------------------------------------------------------
# client registry
# ---------------------------------------------------------------------------


def test_make_client_resolves_names_and_models():
    assert isinstance(make_client("fake"), FakeLLMClient)
    assert isinstance(make_client("claude-cli", claude_bin="/nonexistent/claude"),
                      ClaudeCLIClient)
    client = make_client("claude-cli:opus", claude_bin="/nonexistent/claude")
    assert client.model == "opus"
    explicit = make_client("claude-cli:opus", model="sonnet", claude_bin="/nonexistent/claude")
    assert explicit.model == "sonnet"
    with pytest.raises(LookupError):
        make_client("gpt-9")
    assert {"claude-cli", "fake", "anthropic"} <= set(available_clients())


def test_register_client_adds_a_provider():
    register_client("stub-provider", lambda **kw: FakeLLMClient([{"ok": True}], **kw))
    client = make_client("stub-provider")
    assert client.complete("[role: refiner]", "u") == {"ok": True}


def test_anthropic_client_explains_the_missing_sdk(monkeypatch):
    from framework import llm as llm_module

    monkeypatch.setitem(sys.modules, "anthropic", None)   # import -> ImportError
    with pytest.raises(ImportError, match="pip install anthropic"):
        llm_module.AnthropicAPIClient()
