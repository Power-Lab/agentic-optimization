from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import pandas as pd


@dataclass
class ValidationIssue:
    severity: str
    code: str
    message: str
    path: str | None = None


@dataclass
class ValidationResult:
    status: str
    checks: list[ValidationIssue] = field(default_factory=list)
    input_files: dict[str, str] = field(default_factory=dict)
    candidate_generators: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "checks": [asdict(check) for check in self.checks],
            "input_files": self.input_files,
            "candidate_generators": self.candidate_generators,
        }


def validate_scenario_config(config: dict[str, Any], repo_root: Path) -> ValidationResult:
    result = ValidationResult(status="PASS")
    require_nonempty_string(result, config, "scenario_id")
    validate_input_files(result, config, repo_root)
    validate_candidate_filter(result, config, repo_root)
    validate_solver_settings(result, config)
    validate_expected_outputs(result, config)
    result.status = "FAIL" if any(issue.severity == "FAIL" for issue in result.checks) else "PASS"
    return result


def require_nonempty_string(result: ValidationResult, config: dict[str, Any], key: str) -> None:
    value = config.get(key)
    if not isinstance(value, str) or not value.strip():
        result.checks.append(
            ValidationIssue(
                severity="FAIL",
                code=f"MISSING_{key.upper()}",
                message=f"Scenario config must include a nonempty {key}.",
            )
        )


def validate_input_files(result: ValidationResult, config: dict[str, Any], repo_root: Path) -> None:
    files = normalized_input_files(config)
    if not files:
        result.checks.append(
            ValidationIssue(
                severity="FAIL",
                code="MISSING_INPUT_DATA_FILES",
                message="Scenario config must declare input_data_files.",
            )
        )
        return

    for name, spec in files.items():
        raw_path = spec.get("path")
        if not raw_path:
            result.checks.append(
                ValidationIssue(
                    severity="FAIL",
                    code="MISSING_INPUT_FILE_PATH",
                    message=f"Input file {name} is missing a path.",
                )
            )
            continue

        path = resolve_repo_path(repo_root, raw_path)
        result.input_files[name] = str(path)
        if not path.exists():
            result.checks.append(
                ValidationIssue(
                    severity="FAIL",
                    code="INPUT_FILE_NOT_FOUND",
                    message=f"Input file {name} does not exist.",
                    path=str(path),
                )
            )
            continue

        required_columns = spec.get("required_columns") or []
        if required_columns:
            validate_csv_columns(result, path, name, list(required_columns))
        elif spec.get("expected_fields"):
            result.checks.append(
                ValidationIssue(
                    severity="WARN",
                    code="INPUT_EXPECTED_FIELDS_NOT_ENFORCED",
                    message=(
                        f"Input file {name} declares expected_fields but not required_columns; "
                        "schema-level column validation was skipped."
                    ),
                    path=str(path),
                )
            )

    validate_expected_hour_count(result, config, files, repo_root)


def normalized_input_files(config: dict[str, Any]) -> dict[str, dict[str, Any]]:
    raw = config.get("input_data_files")
    if not isinstance(raw, dict):
        return {}
    return {str(name): spec for name, spec in raw.items() if isinstance(spec, dict)}


def validate_csv_columns(
    result: ValidationResult,
    path: Path,
    name: str,
    required_columns: list[str],
) -> None:
    try:
        columns = set(pd.read_csv(path, nrows=0).columns)
    except Exception as exc:
        result.checks.append(
            ValidationIssue(
                severity="FAIL",
                code="CSV_HEADER_READ_FAILED",
                message=f"Could not read header for input file {name}: {exc}",
                path=str(path),
            )
        )
        return

    missing = sorted(set(required_columns) - columns)
    if missing:
        result.checks.append(
            ValidationIssue(
                severity="FAIL",
                code="CSV_MISSING_COLUMNS",
                message=f"Input file {name} is missing required columns: {missing}",
                path=str(path),
            )
        )


def validate_expected_hour_count(
    result: ValidationResult,
    config: dict[str, Any],
    files: dict[str, dict[str, Any]],
    repo_root: Path,
) -> None:
    expected_count = read_expected_hour_count(config)
    if expected_count is None:
        result.checks.append(
            ValidationIssue(
                severity="WARN",
                code="MISSING_EXPECTED_HOUR_COUNT",
                message="Scenario config does not declare an expected hour count.",
            )
        )
        return

    demand_spec = find_demand_spec(files)
    if demand_spec is None or not demand_spec.get("path"):
        result.checks.append(
            ValidationIssue(
                severity="FAIL",
                code="MISSING_DEMAND_INPUT",
                message="Scenario config must declare a demand input file.",
            )
        )
        return

    path = resolve_repo_path(repo_root, str(demand_spec["path"]))
    if not path.exists():
        return

    try:
        row_count = len(pd.read_csv(path, usecols=[0]))
    except Exception as exc:
        result.checks.append(
            ValidationIssue(
                severity="FAIL",
                code="DEMAND_ROW_COUNT_FAILED",
                message=f"Could not count demand rows: {exc}",
                path=str(path),
            )
        )
        return

    if row_count != expected_count:
        result.checks.append(
            ValidationIssue(
                severity="FAIL",
                code="DEMAND_ROW_COUNT_MISMATCH",
                message=f"Demand has {row_count} rows, expected {expected_count}.",
                path=str(path),
            )
        )


def read_expected_hour_count(config: dict[str, Any]) -> int | None:
    sets = config.get("sets")
    if not isinstance(sets, dict):
        return None

    hours = sets.get("hours") or sets.get("H")
    if isinstance(hours, dict) and isinstance(hours.get("expected_count"), int):
        return int(hours["expected_count"])

    key_parameters = config.get("key_parameters")
    if isinstance(key_parameters, dict) and isinstance(key_parameters.get("planning_horizon_hours"), int):
        return int(key_parameters["planning_horizon_hours"])
    return None


def find_demand_spec(files: dict[str, dict[str, Any]]) -> dict[str, Any] | None:
    for name, spec in files.items():
        if "demand" in name.lower():
            return spec
    return None


def validate_candidate_filter(result: ValidationResult, config: dict[str, Any], repo_root: Path) -> None:
    filter_spec = config.get("candidate_generator_filter")
    if not isinstance(filter_spec, dict):
        result.checks.append(
            ValidationIssue(
                severity="FAIL",
                code="MISSING_CANDIDATE_FILTER",
                message="Scenario config must include candidate_generator_filter.",
            )
        )
        return

    include_values = filter_spec.get("include_values")
    if not isinstance(include_values, list) or not include_values:
        result.checks.append(
            ValidationIssue(
                severity="FAIL",
                code="EMPTY_CANDIDATE_FILTER",
                message="candidate_generator_filter.include_values must contain at least one value.",
            )
        )
        return

    files = normalized_input_files(config)
    generator_spec = find_generator_spec(files)
    if generator_spec is None or not generator_spec.get("path"):
        result.checks.append(
            ValidationIssue(
                severity="FAIL",
                code="MISSING_GENERATOR_INPUT",
                message="Scenario config must declare a generator input file.",
            )
        )
        return

    path = resolve_repo_path(repo_root, str(generator_spec["path"]))
    if not path.exists():
        return

    id_column = (
        filter_spec.get("technology_id_column")
        or filter_spec.get("technology_column")
        or "G"
    )
    matched = matching_candidates(path, str(id_column), [str(value) for value in include_values])
    result.candidate_generators = matched
    if not matched:
        result.checks.append(
            ValidationIssue(
                severity="FAIL",
                code="CANDIDATE_FILTER_MATCHED_NOTHING",
                message=f"Candidate filter matched no rows using column {id_column}.",
                path=str(path),
            )
        )


def find_generator_spec(files: dict[str, dict[str, Any]]) -> dict[str, Any] | None:
    for name, spec in files.items():
        lowered = name.lower()
        if "generator" in lowered or "generators" in lowered:
            return spec
    return None


def matching_candidates(path: Path, id_column: str, include_values: list[str]) -> list[str]:
    frame = pd.read_csv(path)
    include = set(include_values)
    columns_to_try = [id_column, "Description", "technology", "G"]
    matched_indexes = set()
    for column in columns_to_try:
        if column in frame.columns:
            matched = frame[frame[column].astype(str).isin(include)]
            matched_indexes.update(matched.index.tolist())
    if not matched_indexes:
        return []
    matched_frame = frame.loc[sorted(matched_indexes)]
    label_column = "G" if "G" in matched_frame.columns else matched_frame.columns[0]
    return [str(value) for value in matched_frame[label_column].tolist()]


def validate_solver_settings(result: ValidationResult, config: dict[str, Any]) -> None:
    settings = config.get("solver_settings")
    if not isinstance(settings, dict):
        result.checks.append(
            ValidationIssue(
                severity="FAIL",
                code="MISSING_SOLVER_SETTINGS",
                message="Scenario config must include solver_settings.",
            )
        )
        return

    solver_name = settings.get("solver_name")
    if not isinstance(solver_name, str) or not solver_name.strip():
        result.checks.append(
            ValidationIssue(
                severity="FAIL",
                code="MISSING_SOLVER_NAME",
                message="solver_settings.solver_name must be a nonempty string.",
            )
        )

    time_limit = settings.get("time_limit_seconds")
    if not isinstance(time_limit, (int, float)) or time_limit <= 0:
        result.checks.append(
            ValidationIssue(
                severity="FAIL",
                code="INVALID_SOLVER_TIME_LIMIT",
                message="solver_settings.time_limit_seconds must be positive.",
            )
        )


def validate_expected_outputs(result: ValidationResult, config: dict[str, Any]) -> None:
    outputs = config.get("expected_output_files")
    if not isinstance(outputs, dict) or not outputs:
        result.checks.append(
            ValidationIssue(
                severity="FAIL",
                code="MISSING_EXPECTED_OUTPUT_FILES",
                message="Scenario config must include expected_output_files.",
            )
        )
        return

    if "solve_summary" not in outputs:
        result.checks.append(
            ValidationIssue(
                severity="FAIL",
                code="MISSING_SOLVE_SUMMARY_OUTPUT",
                message="expected_output_files must declare solve_summary.",
            )
        )

    for name, spec in outputs.items():
        if not isinstance(spec, dict) or not spec.get("path"):
            result.checks.append(
                ValidationIssue(
                    severity="FAIL",
                    code="MISSING_OUTPUT_PATH",
                    message=f"Expected output {name} is missing a path.",
                )
            )


def resolve_repo_path(repo_root: Path, raw_path: str) -> Path:
    path = Path(raw_path)
    if path.is_absolute():
        return path
    direct = (repo_root / path).resolve()
    if direct.exists():
        return direct
    doc_path = (repo_root / "doc" / path).resolve()
    if doc_path.exists():
        return doc_path
    return direct
