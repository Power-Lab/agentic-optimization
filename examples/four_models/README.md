# Four-model input demo

Show every model's starting inputs:

```bash
python examples/four_models/demo_inputs.py --show-all
```

Change inputs and validate without starting a solver:

```bash
python examples/four_models/demo_inputs.py --model garuda --set scenario=grid --set solver=highs
python examples/four_models/demo_inputs.py --model captive --set scenario=gridcaptive
python examples/four_models/demo_inputs.py --model storage --set storage_gw=40 --set duration_hours=8
python examples/four_models/demo_inputs.py --model resource_adequacy
```

Add `--run` only when the model's Julia environment and solver are ready.
Storage launches 90 periods and resource adequacy launches the complete 54-case
factorial study, so both are intentionally dry-run by default.
