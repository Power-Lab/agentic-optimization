"""``python -m eval`` — run the benchmark, score a results tree, report.

    python -m eval list-tasks --adapter garuda
    python -m eval run   --adapter garuda --llm claude-cli --guardrail both \
                         --seeds 5 --tasks all --max-iters 5 [--dry-run]
    python -m eval score eval_runs [--out eval_runs/report]
    python -m eval report eval_runs [--out eval_runs/report]

Only ``run`` touches an LLM or a solver; ``list-tasks``, ``score`` and
``report`` are pure filesystem work and run in any interpreter. ``--dry-run``
enumerates the cells (and validates each task's config through the adapter when
one can be constructed) without solving or calling a model.

Argparse only — no third-party CLI dependency, so this works on the bare
system python as well as the science env.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, List, Optional, Sequence

from eval.cache import SolveCache
from eval.cells import GUARDRAILS, EvalSetupError, plan_cells, run_cells, supports_unguarded
from eval.report import write_report
from eval.scoring import score
from eval.tasks import REPO_ROOT, discover_tasks, select_tasks, validate_task

DEFAULT_EVAL_ROOT = REPO_ROOT / "eval_runs"
CACHE_DIRNAME = "_cache"
REPORT_DIRNAME = "report"


# ---- helpers ---------------------------------------------------------------

def _load_tasks(adapter: str, spec: Optional[str], parent: Optional[str]):
    tasks = discover_tasks(adapter, parent)
    if not tasks:
        raise SystemExit(
            f"no benchmark tasks found for adapter {adapter!r} under "
            f"{Path(parent) if parent else REPO_ROOT / 'examples'}/{adapter}/benchmark"
        )
    return select_tasks(tasks, spec)


def _get_adapter(name: str) -> Any:
    from framework.registry import available_adapters, get_adapter
    try:
        return get_adapter(name)
    except Exception as e:  # noqa: BLE001 - a missing optional dep is a user error, not a crash
        raise SystemExit(
            f"could not build adapter {name!r}: {type(e).__name__}: {e}\n"
            f"registered adapters: {available_adapters()}"
        )


def _get_client(name: str) -> Any:
    from framework.llm import make_client
    try:
        return make_client(name)
    except Exception as e:  # noqa: BLE001
        raise SystemExit(f"could not build LLM client {name!r}: {type(e).__name__}: {e}")


def _guardrails(arg: str) -> List[str]:
    if arg == "both":
        return list(GUARDRAILS)
    if arg in GUARDRAILS:
        return [arg]
    raise SystemExit(f"--guardrail must be one of both|{'|'.join(GUARDRAILS)}")


# ---- commands --------------------------------------------------------------

def cmd_list_tasks(args: argparse.Namespace) -> int:
    tasks = _load_tasks(args.adapter, args.tasks, args.benchmark_parent)
    if args.json:
        print(json.dumps([t.to_dict() for t in tasks], indent=2, default=str))
        return 0
    width = max((len(t.task_id) for t in tasks), default=8)
    print(f"{len(tasks)} task(s) for adapter {args.adapter!r}:")
    problems = 0
    for t in tasks:
        errs = validate_task(t)
        problems += bool(errs)
        flag = " !!" if errs else ""
        print(f"  {t.task_id:<{width}}  {t.family:<17} "
              f"status={t.label.expected_status or '-':<10} "
              f"tier={t.label.expected_tier or '-'} "
              f"outcome={t.label.expected_terminal_outcome or '-':<11} "
              f"solver={t.needs_solver}"
              f"{' prompt' if t.prompt else ''}{flag}")
        for e in errs:
            print(f"      label error: {e}")
    fams: dict = {}
    for t in tasks:
        fams[t.family] = fams.get(t.family, 0) + 1
    print("  families: " + ", ".join(f"{k}={v}" for k, v in sorted(fams.items())))
    return 1 if problems else 0


def cmd_run(args: argparse.Namespace) -> int:
    tasks = _load_tasks(args.adapter, args.tasks, args.benchmark_parent)
    guardrails = _guardrails(args.guardrail)
    if "unguarded" in guardrails and not supports_unguarded():
        raise SystemExit(
            "this checkout's Supervisor has no enforce_guardrail flag, so the unguarded "
            "condition cannot run; use --guardrail guarded"
        )
    cells = plan_cells(tasks, args.adapter, args.llm, guardrails, args.seeds)
    allowed = [s.strip() for s in args.solvers.split(",")] if args.solvers else None
    if allowed is not None:
        cells = [c for c in cells if c.task.needs_solver in allowed]

    eval_root = Path(args.eval_root)
    print(f"{len(cells)} cell(s): {len(tasks)} task(s) × {len(guardrails)} condition(s) "
          f"× {args.seeds} seed(s) -> {eval_root}")
    if args.dry_run:
        adapter = None
        if not args.no_validate:
            try:
                adapter = _get_adapter(args.adapter)
            except SystemExit as e:
                print(f"(config validation skipped: {e})")
        for c in cells:
            note = ""
            if adapter is not None:
                res = adapter.validate_config(c.task.config)
                note = "  OK" if res.ok else "  INVALID: " + "; ".join(res.errors)
            print(f"  {c.cell_id}{note}")
        return 0

    adapter = _get_adapter(args.adapter)
    client = _get_client(args.llm)
    cache = None if args.no_cache else SolveCache(eval_root / CACHE_DIRNAME)
    results = run_cells(
        cells, adapter, client,
        eval_root=eval_root,
        max_iters=args.max_iters,
        cache=cache,
        progress=print,
        skills_dir=args.skills_dir,
        overwrite=args.overwrite,
    )
    outcomes: dict = {}
    for r in results:
        outcomes[r.outcome] = outcomes.get(r.outcome, 0) + 1
    print("outcomes: " + ", ".join(f"{k}={v}" for k, v in sorted(outcomes.items())))
    if cache is not None:
        print(f"solve cache: {cache.stats()}")
    if args.score:
        return _score_and_report(eval_root, Path(args.out) if args.out else eval_root / REPORT_DIRNAME,
                                 args.benchmark_parent, report=True)
    return 0


def _score_and_report(eval_root: Path, out_dir: Path, benchmark_parent: Optional[str],
                      report: bool) -> int:
    rows, summary = score(eval_root, None, out_dir, benchmark_parent)
    if not rows:
        print(f"no cells found under {eval_root}")
        return 1
    print(f"scored {len(rows)} cell(s) -> {out_dir}")
    if report:
        paths = write_report(rows, summary, out_dir, eval_root=eval_root,
                             benchmark_parent=benchmark_parent)
        for name, p in sorted(paths.items()):
            print(f"  {name}: {p}")
        meta = out_dir / "report.json"
        if meta.exists():
            print(json.loads(meta.read_text()).get("headline", ""))
    return 0


def cmd_score(args: argparse.Namespace) -> int:
    out = Path(args.out) if args.out else Path(args.eval_root) / REPORT_DIRNAME
    return _score_and_report(Path(args.eval_root), out, args.benchmark_parent, args.report)


def cmd_report(args: argparse.Namespace) -> int:
    out = Path(args.out) if args.out else Path(args.eval_root) / REPORT_DIRNAME
    return _score_and_report(Path(args.eval_root), out, args.benchmark_parent, True)


# ---- argument parsing ------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m eval", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="command", required=True)

    def common(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("--benchmark-parent", default=None,
                        help="parent of <adapter>/benchmark (default: <repo>/examples)")

    lt = sub.add_parser("list-tasks", help="list the benchmark tasks of an adapter")
    lt.add_argument("--adapter", required=True)
    lt.add_argument("--tasks", default="all", help="all | id,id,... | family")
    lt.add_argument("--json", action="store_true")
    common(lt)
    lt.set_defaults(func=cmd_list_tasks)

    rn = sub.add_parser("run", help="run cells (task × condition × seed) through the driver")
    rn.add_argument("--adapter", required=True)
    rn.add_argument("--llm", default="claude-cli",
                    help="LLM client name, e.g. claude-cli, claude-cli:opus, fake")
    rn.add_argument("--guardrail", default="both", help="both | guarded | unguarded")
    rn.add_argument("--seeds", type=int, default=5)
    rn.add_argument("--tasks", default="all", help="all | id,id,... | family")
    rn.add_argument("--max-iters", type=int, default=5)
    rn.add_argument("--eval-root", default=str(DEFAULT_EVAL_ROOT))
    rn.add_argument("--solvers", default=None,
                    help="comma list of needs_solver values to include, e.g. highs,none")
    rn.add_argument("--skills-dir", default=None)
    rn.add_argument("--no-cache", action="store_true", help="disable the solve cache")
    rn.add_argument("--overwrite", action="store_true", help="re-run cells that already have cell.json")
    rn.add_argument("--dry-run", action="store_true",
                    help="print the cell plan (and validate configs) without running")
    rn.add_argument("--no-validate", action="store_true",
                    help="with --dry-run, do not build the adapter to validate configs")
    rn.add_argument("--score", action="store_true", help="score and report when the run finishes")
    rn.add_argument("--out", default=None, help="report directory (default <eval-root>/report)")
    common(rn)
    rn.set_defaults(func=cmd_run)

    sc = sub.add_parser("score", help="score a results tree into tidy CSVs")
    sc.add_argument("eval_root")
    sc.add_argument("--out", default=None)
    sc.add_argument("--report", action="store_true", help="also write report.md and the figure")
    common(sc)
    sc.set_defaults(func=cmd_score)

    rp = sub.add_parser("report", help="score + write report.md, the tables and the figure")
    rp.add_argument("eval_root")
    rp.add_argument("--out", default=None)
    common(rp)
    rp.set_defaults(func=cmd_report)
    return p


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(list(argv) if argv is not None else None)
    try:
        return int(args.func(args) or 0)
    except EvalSetupError as e:
        print(f"setup error: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
