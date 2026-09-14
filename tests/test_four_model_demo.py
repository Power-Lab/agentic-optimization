from examples.four_models.demo_inputs import PRESETS, apply_overrides, parse_value


def test_demo_has_four_power_lab_models():
    assert set(PRESETS) == {"garuda", "captive", "storage", "resource_adequacy"}


def test_demo_overrides_numeric_and_string_inputs():
    updated = apply_overrides(PRESETS["storage"], ["storage_gw=40", "duration_hours=8"])
    assert updated["storage_gw"] == 40
    assert updated["duration_hours"] == 8
    assert parse_value("gridcaptive") == "gridcaptive"
