#!/usr/bin/env python3
"""Probe a compute node before running anything heavy on it.

Checks reachability, julia, the Gurobi licence env, and whether the garuda
checkout is present at --remote-root (offering to clone + bootstrap it with
--ensure-repo from https://github.com/kaarthi19/garuda.git).

    python examples/garuda/remote_check.py --host pwrlab --remote-root '~/garuda'
    python examples/garuda/remote_check.py --host user@node --remote-root '~/garuda' --ensure-repo
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from adapters.garuda import RemoteGarudaAdapter  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", required=True, help="ssh target, e.g. user@node or an ssh alias")
    ap.add_argument("--remote-root", default="~/garuda",
                    help="path to the garuda checkout on the node (default ~/garuda)")
    ap.add_argument("--julia", default="julia", help="julia executable on the node")
    ap.add_argument("--ensure-repo", action="store_true",
                    help="clone + bootstrap the model on the node if absent")
    args = ap.parse_args()

    adapter = RemoteGarudaAdapter(ssh_host=args.host, remote_root=args.remote_root,
                                  julia=args.julia)

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
