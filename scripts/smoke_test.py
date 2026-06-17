#!/usr/bin/env python3
"""Phase 1 gate: prove the framework can drive the real model end-to-end.

Runs ``base_maluku_2030_reference`` live through the VillageAdapter and checks:
  1. a ``solver.log`` was captured,
  2. the run terminated OPTIMAL,
  3. the produced CSVs match the model's committed baseline (within tolerance).

Must pass before any skill is built on top of the runner.

Usage:
    python scripts/smoke_test.py [--no-bootstrap] [--tol 1e-6]
"""

from __future__ import annotations

import argparse
import csv
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from adapters.village import VillageAdapter  # noqa: E402
from framework import run_and_record  # noqa: E402

FRAMEWORK_ROOT = Path(__file__).resolve().parents[1]
MODEL_ROOT = FRAMEWORK_ROOT / "models" / "village"
RESULTS_NAME = "base_maluku_2030_reference"
RUN_DIR = FRAMEWORK_ROOT / "runs" / "smoke_base_maluku"

CONFIG = {
    "island": "maluku",
    "year": "2030",
    "scenario": "base",
    "clean": "reference",
    "CO235reduction": False,
    "BAUCO2emissions": 0.0,
    "CO2_limit": 5820000,
}


def bootstrap() -> None:
    print("[smoke] Bootstrapping Julia environment (one-time, ~1 min)…")
    proc = subprocess.run(
        ["julia", f"--project={MODEL_ROOT}", "bootstrap.jl"],
        cwd=MODEL_ROOT,
    )
    if proc.returncode != 0:
        sys.exit("[smoke] FAIL: Julia bootstrap failed.")


def snapshot_baseline() -> Path:
    """Copy the model's committed baseline aside before the run overwrites it."""
    src = MODEL_ROOT / "results" / RESULTS_NAME
    backup = FRAMEWORK_ROOT / "runs" / "_baseline_committed"
    if backup.exists():
        shutil.rmtree(backup)
    shutil.copytree(src, backup)
    return backup


def _to_float(cell: str):
    try:
        return float(cell)
    except (TypeError, ValueError):
        return None


def compare_csv(produced: Path, baseline: Path, tol: float) -> list[str]:
    diffs: list[str] = []
    with produced.open() as f:
        prod_rows = list(csv.reader(f))
    with baseline.open() as f:
        base_rows = list(csv.reader(f))
    if len(prod_rows) != len(base_rows):
        return [f"{produced.name}: row count {len(prod_rows)} != baseline {len(base_rows)}"]
    for r, (pr, br) in enumerate(zip(prod_rows, base_rows)):
        if len(pr) != len(br):
            diffs.append(f"{produced.name}: row {r} column count differs")
            continue
        for c, (pcell, bcell) in enumerate(zip(pr, br)):
            pf, bf = _to_float(pcell), _to_float(bcell)
            if pf is not None and bf is not None:
                denom = max(1.0, abs(bf))
                if abs(pf - bf) / denom > tol:
                    diffs.append(f"{produced.name}: r{r}c{c} {pf} vs {bf}")
            elif pcell != bcell:
                diffs.append(f"{produced.name}: r{r}c{c} {pcell!r} vs {bcell!r}")
    return diffs


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-bootstrap", action="store_true")
    ap.add_argument("--tol", type=float, default=1e-4,
                    help="relative tolerance for numeric CSV cells")
    args = ap.parse_args()

    if not (MODEL_ROOT / "run_model.jl").exists():
        sys.exit(f"[smoke] FAIL: model not found at {MODEL_ROOT}. Init the submodule.")

    if not args.no_bootstrap:
        bootstrap()

    baseline = snapshot_baseline()
    print(f"[smoke] Baseline snapshot: {baseline}")

    adapter = VillageAdapter(model_root=MODEL_ROOT)
    print(f"[smoke] Running {RESULTS_NAME} live…")
    record = run_and_record(adapter, CONFIG, RUN_DIR, preflight=True)

    ok = True

    # Check 1: solver.log captured.
    log = RUN_DIR / "solver.log"
    if log.exists() and log.stat().st_size > 0:
        print(f"[smoke] PASS: solver.log captured ({log.stat().st_size} bytes)")
    else:
        ok = False
        print("[smoke] FAIL: solver.log missing or empty")

    # Check 2: terminated OPTIMAL.
    status = record.execution.termination_status
    if status == "OPTIMAL":
        print(f"[smoke] PASS: termination_status = OPTIMAL ({record.execution.wall_seconds}s)")
    else:
        ok = False
        print(f"[smoke] FAIL: termination_status = {status}")
        print((RUN_DIR / "solver.log").read_text()[-2000:])

    # Check 3: outputs match the committed baseline.
    outputs = adapter.locate_outputs(RUN_DIR)
    if not outputs:
        ok = False
        print("[smoke] FAIL: no output CSVs archived")
    else:
        all_diffs: list[str] = []
        for name, path in outputs.items():
            base_csv = baseline / path.name
            if not base_csv.exists():
                all_diffs.append(f"{path.name}: no baseline counterpart")
                continue
            all_diffs.extend(compare_csv(path, base_csv, args.tol))
        if all_diffs:
            ok = False
            print(f"[smoke] FAIL: {len(all_diffs)} CSV cell diffs (tol={args.tol}):")
            for d in all_diffs[:20]:
                print("   ", d)
        else:
            print(f"[smoke] PASS: {len(outputs)} output CSVs match baseline (tol={args.tol})")

    print("\n[smoke]", "✅ ALL CHECKS PASSED" if ok else "❌ SMOKE TEST FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
