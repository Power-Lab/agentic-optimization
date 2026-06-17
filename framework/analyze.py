"""Output-analysis helpers (Task 4) — model-agnostic.

The output-analyzer skill does the reasoning (is this dispatch plausible? why
are emissions high?); these helpers just load the outputs through the adapter
(so no model filenames are hard-coded) and record anomalies into the run record.
"""

from __future__ import annotations

import csv
from pathlib import Path
from typing import Dict, List

from framework.adapter import Adapter
from framework.run_record import Anomaly, RunRecord


def read_outputs(adapter: Adapter, run_dir: str | Path) -> Dict[str, List[dict]]:
    """Load every output the adapter exposes as a list of row dicts.

    Keyed by the adapter's stable output name (from ``locate_outputs``), so the
    analyzer reasons about "cost_results" etc. without knowing file paths.
    """
    outputs: Dict[str, List[dict]] = {}
    for name, path in adapter.locate_outputs(Path(run_dir)).items():
        with Path(path).open() as f:
            outputs[name] = list(csv.DictReader(f))
    return outputs


def record_anomalies(
    record: RunRecord,
    anomalies: List[Anomaly],
    run_dir: str | Path | None = None,
) -> RunRecord:
    """Append anomalies to the run record (and persist if a run_dir is given)."""
    record.output_anomalies.extend(anomalies)
    if run_dir is not None:
        record.save(Path(run_dir) / "run_record.json")
    return record
