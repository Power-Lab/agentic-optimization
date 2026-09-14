"""Contract tests for the three additional Power Lab model adapters."""

from pathlib import Path

from adapters.captive import CaptiveIndonesiaAdapter
from adapters.resource_adequacy import ResourceAdequacyAdapter
from adapters.storage import EnergyStorageAdapter
from framework.registry import available_adapters


def test_all_six_bundled_adapters_register():
    assert {"garuda", "captive", "storage", "resource_adequacy", "pathways", "pypsa_toy"} <= set(available_adapters())


def test_captive_schema_and_policy_tiers(tmp_path: Path):
    (tmp_path / "data_indonesia" / "2030" / "maluku").mkdir(parents=True)
    (tmp_path / "run_model.jl").touch()
    adapter = CaptiveIndonesiaAdapter(model_root=tmp_path)
    config = {"island": "maluku", "year": "2030", "scenario": "base", "clean": "reference",
              "CO235reduction": False, "BAUCO2emissions": 0.0, "CO2_limit": 5_820_000.0}
    assert adapter.validate_config(config).ok
    assert {"clean", "CO2_limit"} <= adapter.intervention_spec().tier_c_keys


def test_storage_run_name_validation_and_tiers(tmp_path: Path):
    runner = tmp_path / "code" / "run_all_periods.jl"
    runner.parent.mkdir()
    runner.touch()
    adapter = EnergyStorageAdapter(model_root=tmp_path)
    config = {"storage_gw": 20, "duration_hours": 4, "wind_scale": 3,
              "solar_scale": 3, "simulation_days": 4, "production_incentive": 10}
    assert adapter.validate_config(config).ok
    assert adapter.run_name(config) == "b20_hrs4_w3_s3_days4_ptc10"
    assert "production_incentive" in adapter.intervention_spec().tier_c_keys


def test_resource_adequacy_exposes_only_real_batch_control(tmp_path: Path):
    runner = tmp_path / "uced_neg_2030" / "uced-model" / "R1_Run.jl"
    runner.parent.mkdir(parents=True)
    runner.touch()
    adapter = ResourceAdequacyAdapter(model_root=tmp_path)
    assert adapter.validate_config({"study": "2030_full_factorial"}).ok
    assert not adapter.validate_config({"study": "2030_full_factorial", "loadgrowth": "growth1"}).ok
