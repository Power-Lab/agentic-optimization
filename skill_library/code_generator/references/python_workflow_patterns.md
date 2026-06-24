# Python Workflow Patterns

## CLI Structure

Use `argparse` and `pathlib` for scripts that run in local and HPC environments:

```python
from argparse import ArgumentParser
from pathlib import Path

def parse_args():
    parser = ArgumentParser()
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--out-dir", type=Path, required=True)
    return parser.parse_args()
```

## Config Loading

Use `yaml.safe_load` for YAML and `json.load` for JSON. Validate required keys before running solvers.

```python
import yaml

def load_yaml(path):
    with path.open("r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle)
    if not isinstance(data, dict):
        raise ValueError(f"Expected mapping in {path}")
    return data
```

## Subprocess Execution

Use argument lists, not shell strings:

```python
import subprocess

result = subprocess.run(
    ["julia", "--project=.", "src/run_model.jl", "--config", str(config_path)],
    cwd=project_dir,
    text=True,
    capture_output=True,
    check=False,
)
```

Write stdout and stderr to logs, then raise a concise error with command and return code.

## Structured Outputs

Write machine-readable run metadata:

```python
summary = {
    "status": "FAILED" if result.returncode else "COMPLETED",
    "return_code": result.returncode,
    "config": str(config_path),
    "stdout_log": str(stdout_log),
    "stderr_log": str(stderr_log),
}
```

Use stable keys so downstream analyzers can consume results across iterations.

## Parameter Sweeps

Generate one config per run and a manifest:
- `run_id`
- scenario overrides
- config path
- expected output directory
- status

Keep generated configs deterministic by sorting keys and avoiding timestamps in IDs unless the workflow requires uniqueness.

