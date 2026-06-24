#!/usr/bin/env bash
#SBATCH --job-name=energy-dispatch
#SBATCH --partition=compute
#SBATCH --time=01:00:00
#SBATCH --cpus-per-task=4
#SBATCH --mem=8G
#SBATCH --output=logs/%x-%j.out
#SBATCH --error=logs/%x-%j.err

set -euo pipefail

echo "job_id=${SLURM_JOB_ID:-local}"
echo "host=$(hostname)"
echo "start=$(date --iso-8601=seconds)"

mkdir -p logs results

module purge
module load julia
module load gurobi

python assets/example_runner.py \
  --config configs/scenario_config.yaml \
  --out-dir results/base_case \
  --project-dir "$PWD"

echo "end=$(date --iso-8601=seconds)"
