"""Output-analysis plumbing (Task 4) — no solver needed.

Confirms read_outputs loads through the adapter's locate_outputs (no hard-coded
filenames) and record_anomalies appends to + persists the run record.
"""

from pathlib import Path

from framework import Anomaly, RunRecord, read_outputs, record_anomalies
from framework.adapter import Adapter


class _OutAdapter(Adapter):
    """Minimal adapter exposing only output location, for analysis tests."""
    name = "outmock"

    def validate_config(self, config):  # pragma: no cover - unused
        ...

    def run(self, config, run_dir):  # pragma: no cover - unused
        ...

    def intervention_spec(self):  # pragma: no cover - unused
        from framework import InterventionSpec
        return InterventionSpec()

    def locate_outputs(self, run_dir):
        out = Path(run_dir) / "outputs"
        return {p.stem: p for p in sorted(out.glob("*.csv"))}


def test_read_outputs_loads_rows(tmp_path):
    out = tmp_path / "outputs"
    out.mkdir()
    (out / "cost_results.csv").write_text("Total_Costs,NSE_Costs\n87.8,2.7\n")
    outputs = read_outputs(_OutAdapter(), tmp_path)
    assert "cost_results" in outputs
    assert outputs["cost_results"][0]["Total_Costs"] == "87.8"


def test_record_anomalies_persists(tmp_path):
    rec = RunRecord(config={"island": "x"})
    record_anomalies(
        rec,
        [Anomaly(metric="Total_NSE_MWh", value=1234.0, expected="~0", severity="high")],
        run_dir=tmp_path,
    )
    reloaded = RunRecord.load(tmp_path / "run_record.json")
    assert reloaded.output_anomalies[0].metric == "Total_NSE_MWh"
    assert reloaded.output_anomalies[0].severity == "high"
