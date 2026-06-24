from __future__ import annotations

import copy
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import yaml


SAFE_TOP_LEVEL_KEYS = {"solver_settings"}
UNSUPPORTED_SAFE_KEYS = {"warm_start", "NumericFocus", "ScaleFlag"}


@dataclass
class PatchResult:
    status: str
    reason: str
    patched_config_path: str | None = None
    applied_overrides: dict[str, Any] | None = None
    skipped_overrides: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def create_safe_patched_config(
    base_config: dict[str, Any],
    refinement: dict[str, Any],
    run_dir: Path,
    iteration: int,
) -> tuple[dict[str, Any] | None, PatchResult]:
    overrides = refinement.get("refinement_overrides")
    if not isinstance(overrides, dict) or not overrides:
        return None, PatchResult(status="SKIPPED", reason="No refinement overrides were provided.")

    unsafe_keys = sorted(set(overrides) - SAFE_TOP_LEVEL_KEYS)
    if unsafe_keys:
        return None, PatchResult(
            status="SKIPPED",
            reason=f"Overrides include non-safe top-level keys: {unsafe_keys}.",
            skipped_overrides=overrides,
        )

    patched = copy.deepcopy(base_config)
    applied: dict[str, Any] = {}
    skipped: dict[str, Any] = {}

    solver_overrides = overrides.get("solver_settings", {})
    if isinstance(solver_overrides, dict):
        applied_solver, skipped_solver = apply_solver_settings_patch(patched, solver_overrides)
        if applied_solver:
            applied["solver_settings"] = applied_solver
        if skipped_solver:
            skipped["solver_settings"] = skipped_solver

    if not applied:
        return None, PatchResult(
            status="SKIPPED",
            reason="No supported safe overrides were available to apply.",
            skipped_overrides=skipped or overrides,
        )

    patch_iteration_output_paths(patched, run_dir, iteration)
    patched_path = run_dir / f"iteration_{iteration}" / "scenario_config.yaml"
    patched_path.parent.mkdir(parents=True, exist_ok=True)
    patched_path.write_text(yaml.safe_dump(patched, sort_keys=False), encoding="utf-8")
    return patched, PatchResult(
        status="APPLIED",
        reason="Applied safe solver-setting overrides and redirected output files for rerun.",
        patched_config_path=str(patched_path),
        applied_overrides=applied,
        skipped_overrides=skipped or None,
    )


def apply_solver_settings_patch(
    config: dict[str, Any],
    solver_overrides: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    settings = config.setdefault("solver_settings", {})
    if not isinstance(settings, dict):
        config["solver_settings"] = {}
        settings = config["solver_settings"]

    applied: dict[str, Any] = {}
    skipped: dict[str, Any] = {}
    for key, value in solver_overrides.items():
        if key in UNSUPPORTED_SAFE_KEYS:
            skipped[key] = value
            continue
        if key == "time_limit_seconds" and is_positive_number(value):
            settings[key] = float(value)
            applied[key] = float(value)
        elif key == "relative_mip_gap" and is_nonnegative_number(value):
            settings[key] = float(value)
            applied[key] = float(value)
        elif key in {"presolve", "log_to_console", "log_file"}:
            settings[key] = value
            applied[key] = value
        else:
            skipped[key] = value
    return applied, skipped


def patch_iteration_output_paths(config: dict[str, Any], run_dir: Path, iteration: int) -> None:
    output_files = config.get("expected_output_files")
    if not isinstance(output_files, dict):
        return
    relative_base = Path("runs") / run_dir.name / f"iteration_{iteration}" / "outputs"
    for name, spec in output_files.items():
        if not isinstance(spec, dict) or not spec.get("path"):
            continue
        filename = Path(str(spec["path"])).name
        spec["path"] = str(relative_base / filename).replace("\\", "/")

    settings = config.get("solver_settings")
    if isinstance(settings, dict) and settings.get("log_file"):
        settings["log_file"] = str((relative_base / Path(str(settings["log_file"])).name)).replace("\\", "/")


def is_positive_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and value > 0


def is_nonnegative_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and value >= 0
