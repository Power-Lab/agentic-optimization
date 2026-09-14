import pytest

from framework.power_lab_agent import build_config, plan, route


@pytest.mark.parametrize(("prompt", "expected"), [
    ("Analyze a 20 GW four-hour battery", "storage"),
    ("Run the 2030 Northeast China resource adequacy study", "resource_adequacy"),
    ("Create a reference captive scenario for Maluku", "captive"),
    ("Create a coordinated grid and village clean-energy scenario", "garuda"),
])
def test_routes_four_models(prompt, expected):
    assert route(prompt)[0] == expected


def test_storage_prompt_extracts_inputs():
    config = build_config("storage", "Analyze a 40 GW eight-hour battery with wind scale 7 and solar scale 5")
    assert config["storage_gw"] == 40
    assert config["duration_hours"] == 8
    assert config["wind_scale"] == 7
    assert config["solar_scale"] == 5


def test_plan_validates_real_adapter_checkout():
    result = plan("Create a reference captive scenario for Maluku in 2030")
    assert result["selected_model"] == "captive"
    assert result["validation"]["ok"]
    assert result["execution_authorized"] is False


def test_garuda_village_prompt_uses_complete_demo_dataset():
    result = plan("Create a coordinated grid and village clean-energy scenario in 2030")
    assert result["selected_model"] == "garuda"
    assert result["config"]["island"] == "timor_demo"
    assert result["validation"]["ok"]


def test_ambiguous_prompt_requires_model_override():
    with pytest.raises(ValueError):
        route("Please optimize this case")
