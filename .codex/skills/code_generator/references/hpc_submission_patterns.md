# HPC Submission Patterns

## sbatch Basics

Use explicit resources and logs:

```bash
#!/usr/bin/env bash
#SBATCH --job-name=energy-model
#SBATCH --partition=compute
#SBATCH --time=02:00:00
#SBATCH --cpus-per-task=8
#SBATCH --mem=16G
#SBATCH --output=logs/%x-%j.out
#SBATCH --error=logs/%x-%j.err
```

## Script Body

Use strict mode, echo key context, and create output directories:

```bash
set -euo pipefail

echo "job_id=${SLURM_JOB_ID:-local}"
echo "host=$(hostname)"
echo "start=$(date --iso-8601=seconds)"

mkdir -p logs results
```

Load modules and activate environments before running the model. Keep site-specific commands grouped and easy to edit.

## Job Arrays

For parameter sweeps, use a manifest and `SLURM_ARRAY_TASK_ID`:

```bash
#SBATCH --array=0-49
CONFIG=$(sed -n "$((SLURM_ARRAY_TASK_ID + 1))p" configs/manifest.txt)
python scripts/run_model.py --config "$CONFIG" --out-dir "results/${SLURM_ARRAY_TASK_ID}"
```

## Debugging Failed Jobs

Check:
- Whether modules and environments load in non-interactive shells
- Whether relative paths resolve from the submission directory
- Whether log directories exist before SLURM writes logs
- Whether the Gurobi license is visible on compute nodes
- Whether memory, wall time, or thread limits match solver settings

