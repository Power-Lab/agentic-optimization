# Model Executor — Scripts

The Model Executor's core logic lives in `scripts/submit_model_run.py` (root-level library). This folder documents the extension points for plugging in a real solver.

## Mock Mode (already working)

```bash
python scripts/submit_model_run.py \
    --config runs/run_001/scenario_config.yaml \
    --run-dir runs/run_001/ \
    --mode mock
```

## Julia/JuMP Extension Point

To wire a real Julia/JuMP/Gurobi solver, implement the following function in `scripts/submit_model_run.py`:

```python
def run_julia_local(config: dict, run_dir: Path) -> dict:
    """Run Julia/JuMP model locally."""
    import subprocess
    model_script = "model/unit_commitment.jl"
    config_path = run_dir / "scenario_config.yaml"
    log_path = run_dir / "solver.log"
    cmd = [
        "julia", "--threads=auto", model_script,
        "--config", str(config_path),
        "--output-dir", str(run_dir),
    ]
    with open(log_path, "w") as logf:
        proc = subprocess.Popen(cmd, stdout=logf, stderr=logf)
    return {"job_id": f"local_pid_{proc.pid}", "status": "RUNNING"}
```

## Slurm HPC Extension Point

```python
def submit_slurm_job(config: dict, run_dir: Path) -> dict:
    """Submit Julia/JuMP model to a Slurm cluster."""
    import subprocess
    hpc = config.get("hpc_config", {})
    job_script = run_dir / "job.sh"
    with open(job_script, "w") as f:
        f.write(f"""#!/bin/bash
#SBATCH --partition={hpc.get('partition', 'compute')}
#SBATCH --nodes={hpc.get('nodes', 1)}
#SBATCH --cpus-per-task={hpc.get('cpus_per_task', 8)}
#SBATCH --mem={hpc.get('memory_GB', 32)}G
#SBATCH --time={hpc.get('walltime_h', 2)}:00:00
module load julia gurobi
julia --threads={hpc.get('julia_threads', 8)} model/unit_commitment.jl \\
    --config {run_dir}/scenario_config.yaml \\
    --output-dir {run_dir}
""")
    result = subprocess.run(["sbatch", str(job_script)], capture_output=True, text=True)
    job_id = result.stdout.strip().split()[-1]
    return {"job_id": f"slurm_{job_id}", "status": "SUBMITTED"}
```

## Inject Failure Modes (for testing)

```bash
# Test infeasible path
python scripts/submit_model_run.py --config ... --run-dir ... --mode mock --inject-issue infeasible

# Test timeout path
python scripts/submit_model_run.py --config ... --run-dir ... --mode mock --inject-issue timeout

# Test numerical instability path
python scripts/submit_model_run.py --config ... --run-dir ... --mode mock --inject-issue numerical
```
