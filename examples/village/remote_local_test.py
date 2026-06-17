#!/usr/bin/env python3
"""Local loopback test of the REMOTE backend, before any real SSH node.

Runs base_maluku through RemoteVillageAdapter with a LocalTransport: the full
remote orchestration (marshal config -> mkdir job dir -> push config -> run in
remote_root -> pull results back) executes on this machine, swapping only the
ssh binary for a local shell. Confirms the backend is mechanically sound and
that pulled-back outputs match the committed baseline.

    python examples/village/remote_local_test.py
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(HERE))  # for the sibling smoke_test module

from adapters.village import RemoteVillageAdapter  # noqa: E402
from framework import run_and_record               # noqa: E402
from smoke_test import CONFIG, compare_csv          # noqa: E402

MODEL_ROOT = ROOT / "models" / "village"
RESULTS_NAME = "base_maluku_2030_reference"
RUN_DIR = ROOT / "runs" / "remote_local_base_maluku"
TOL = 1e-4


def main() -> int:
    # Snapshot the model's current baseline before the run overwrites it.
    src = MODEL_ROOT / "results" / RESULTS_NAME
    backup = ROOT / "runs" / "_baseline_remote_local"
    if backup.exists():
        shutil.rmtree(backup)
    shutil.copytree(src, backup)

    adapter = RemoteVillageAdapter.local_loopback(remote_root=str(MODEL_ROOT))
    print(f"[remote-local] transport = {adapter.transport.description}")
    print(f"[remote-local] remote_root = {adapter.remote_root}")

    info = adapter.check_connection()
    print("[remote-local] probe:")
    for line in info["stdout"].strip().splitlines():
        print("   ", line)

    print(f"[remote-local] running {RESULTS_NAME} via the remote orchestration…")
    record = run_and_record(adapter, CONFIG, RUN_DIR, preflight=True)

    ok = True
    log = RUN_DIR / "solver.log"
    if log.exists() and log.stat().st_size > 0:
        print(f"[remote-local] PASS: solver.log pulled/captured ({log.stat().st_size} bytes)")
    else:
        ok = False
        print("[remote-local] FAIL: solver.log missing")

    status = record.execution.termination_status
    if status == "OPTIMAL":
        print(f"[remote-local] PASS: OPTIMAL ({record.execution.wall_seconds}s)")
    else:
        ok = False
        print(f"[remote-local] FAIL: status = {status}")

    outputs = adapter.locate_outputs(RUN_DIR)
    if not outputs:
        ok = False
        print("[remote-local] FAIL: no outputs pulled back")
    else:
        diffs = []
        for _, path in outputs.items():
            base = backup / path.name
            if base.exists():
                diffs.extend(compare_csv(path, base, TOL))
        if diffs:
            ok = False
            print(f"[remote-local] FAIL: {len(diffs)} cell diffs vs baseline")
            for d in diffs[:15]:
                print("   ", d)
        else:
            print(f"[remote-local] PASS: {len(outputs)} pulled CSVs match baseline (tol={TOL})")

    print("\n[remote-local]", "✅ REMOTE BACKEND OK (loopback)" if ok else "❌ FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
