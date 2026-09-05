#!/usr/bin/env python3
"""Local loopback test of the REMOTE backend, before any real SSH node.

Runs the CI regression case (maluku 2030 base dispatch, HiGHS) through
RemoteGarudaAdapter with a LocalTransport: the full remote orchestration
(marshal config -> mkdir job dir -> push config -> run in remote_root -> pull
results back) executes on this machine, swapping only the ssh binary for a local
shell. Confirms the backend is mechanically sound and that the pulled-back
outputs reproduce the model's headline numbers (+-1 %).

    python examples/garuda/remote_local_test.py [--remote-root PATH] [--run-dir DIR]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(ROOT))

from adapters.garuda import (  # noqa: E402
    REGRESSION_CONFIG, REGRESSION_TOLERANCE, RemoteGarudaAdapter, check_regression_headlines,
)
from framework import run_and_record  # noqa: E402

MODEL_ROOT = ROOT / "models" / "garuda"
DEFAULT_RUN_DIR = ROOT / "runs" / "remote_local_garuda_maluku_dispatch"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--remote-root", default=str(MODEL_ROOT),
                    help="the 'remote' checkout to run in (default: the submodule)")
    ap.add_argument("--run-dir", default=str(DEFAULT_RUN_DIR))
    ap.add_argument("--skip-schema-validation", action="store_true")
    args = ap.parse_args()
    run_dir = Path(args.run_dir)

    remote_env = {"GARUDA_SKIP_VALIDATION": "1"} if args.skip_schema_validation else None
    adapter = RemoteGarudaAdapter.local_loopback(remote_root=args.remote_root,
                                                 remote_env=remote_env)
    print(f"[remote-local] transport = {adapter.transport.description}")
    print(f"[remote-local] remote_root = {adapter.remote_root}")

    info = adapter.check_connection()
    print("[remote-local] probe:")
    for line in info["stdout"].strip().splitlines():
        print("   ", line)

    config = dict(REGRESSION_CONFIG)
    print(f"[remote-local] running {adapter.results_name(config)} via the remote orchestration…")
    record = run_and_record(adapter, config, run_dir, preflight=True)

    ok = True
    log = run_dir / "solver.log"
    if log.exists() and log.stat().st_size > 0:
        print(f"[remote-local] PASS: solver.log captured ({log.stat().st_size} bytes)")
    else:
        ok = False
        print("[remote-local] FAIL: solver.log missing")

    status = record.execution.termination_status
    if status == "OPTIMAL":
        print(f"[remote-local] PASS: OPTIMAL ({record.execution.wall_seconds}s)")
    else:
        ok = False
        print(f"[remote-local] FAIL: status = {status} (origin={record.execution.error_origin})")
        if log.exists():
            print(log.read_text()[-1500:])

    outputs = adapter.locate_outputs(run_dir)
    if not outputs:
        ok = False
        print("[remote-local] FAIL: no outputs pulled back")
    else:
        for metric, (got, want, within) in check_regression_headlines(outputs, REGRESSION_TOLERANCE).items():
            print(f"[remote-local] {'PASS' if within else 'FAIL'}: {metric} = {got} (expected {want})")
            ok = ok and within
        print(f"[remote-local] pulled {len(outputs)} CSVs")

    print("\n[remote-local]", "REMOTE BACKEND OK (loopback)" if ok else "FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
