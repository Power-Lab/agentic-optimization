from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path

import yaml


def load_config(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as handle:
        config = yaml.safe_load(handle)
    if not isinstance(config, dict):
        raise ValueError(f"Expected YAML mapping in {path}")
    for key in ("demand_mw", "generators"):
        if key not in config:
            raise ValueError(f"Missing required config key: {key}")
    return config


def run_model(config_path: Path, out_dir: Path, project_dir: Path) -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)
    stdout_log = out_dir / "julia_stdout.log"
    stderr_log = out_dir / "julia_stderr.log"

    command = ["julia", "--project=.", "assets/example_jump_model.jl", str(config_path)]
    result = subprocess.run(
        command,
        cwd=project_dir,
        text=True,
        capture_output=True,
        check=False,
    )

    stdout_log.write_text(result.stdout, encoding="utf-8")
    stderr_log.write_text(result.stderr, encoding="utf-8")

    summary = {
        "status": "COMPLETED" if result.returncode == 0 else "FAILED",
        "return_code": result.returncode,
        "command": command,
        "config_path": str(config_path),
        "stdout_log": str(stdout_log),
        "stderr_log": str(stderr_log),
    }
    (out_dir / "run_summary.json").write_text(
        json.dumps(summary, indent=2),
        encoding="utf-8",
    )
    return summary


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run an energy model scenario.")
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--out-dir", type=Path, default=Path("results"))
    parser.add_argument("--project-dir", type=Path, default=Path("."))
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    config_path = args.config.resolve()
    project_dir = args.project_dir.resolve()
    load_config(config_path)
    summary = run_model(config_path, args.out_dir.resolve(), project_dir)
    return int(summary["return_code"])


if __name__ == "__main__":
    raise SystemExit(main())

