#!/usr/bin/env python3
"""Probe the colo node before running anything heavy on it.

Checks reachability, julia, the Gurobi licence env, and whether the model repo
is present at --remote-root (offering to clone it with --ensure-repo).

    python examples/village/remote_check.py --host user@colo --remote-root '~/agentic-optimization/models/village'
    python examples/village/remote_check.py --host user@colo --remote-root '~/village' --ensure-repo
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from adapters.village import RemoteVillageAdapter  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", required=True, help="ssh target, e.g. user@colo")
    ap.add_argument("--remote-root", required=True,
                    help="path to the village model checkout on the node")
    ap.add_argument("--ensure-repo", action="store_true",
                    help="clone + bootstrap the model on the node if absent")
    args = ap.parse_args()

    adapter = RemoteVillageAdapter(ssh_host=args.host, remote_root=args.remote_root)

    print(f"[remote] probing {args.host} …")
    info = adapter.check_connection()
    if info["returncode"] != 0:
        print(f"[remote] FAIL: ssh returned {info['returncode']}")
        print(info["stderr"].strip())
        return 1
    print(info["stdout"].strip())

    if args.ensure_repo:
        print("[remote] ensuring repo present (clone + bootstrap if needed)…")
        r = adapter.ensure_remote_repo()
        print(r.stdout.strip() or r.stderr.strip())
        if r.returncode != 0:
            return 1

    print("[remote] OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
