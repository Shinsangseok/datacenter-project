# MLflow Projects training

`MLproject` defines project `datacenter-anomaly-training` and entry point `train`.
Projects translates typed parameters into the existing `train_model.py` CLI;
training logic, synthetic dataset, feature/target contract and output paths are
unchanged. This is an execution interface, not model deployment or registration.

## Environment and tracking

From the repository root, activate the already validated environment:

```sh
source .venv/bin/activate
```

It must already have `requirements-mlflow.txt` installed, including MLflow 3.16.1.
Use `--env-manager local` explicitly: no new conda/virtualenv environment or Docker
image is created, and the entry point's `python` is resolved through PATH.
This does not lock transitive dependencies, OS libraries or the interpreter;
reproducibility depends on preserving the existing environment and input data.

Set `MLFLOW_TRACKING_URI` externally to the independently approved environment
and access path. The read-only inference client cannot perform training writes;
do not distribute administrator credentials to bypass that boundary. No URI or
credential is stored in MLproject. Do not invoke Projects without setting the
tracking environment. The commands below create runs and upload artifacts;
source integration and offline tests do not authorize running them against DEV
or PROD. Localhost forwarding alone does not establish MLflow authorization.

## Run

```sh
mlflow run . -e train --env-manager local \
  --experiment-name datacenter-anomaly-detection

mlflow run . -e train --env-manager local \
  --experiment-name datacenter-anomaly-detection -P max_depth=6
```

The experiment option is required to match the experiment selected by the training
code. Projects creates a run and passes `MLFLOW_RUN_ID`; the existing `start_run`
resumes that run, so each Project execution has one training run rather than a
separate nested run. Do not supply `--run-id` or set `MLFLOW_RUN_ID` manually.

Defaults are n_estimators=100, max_depth=10, class_weight=balanced,
random_state=42, test_size=0.2 and n_jobs=2. CLI validation remains authoritative.
The data is synthetic; metrics are experimental validation, not production claims.

Local outputs are overwritten at `models/model.pkl`, `reports/metrics.json` and
`reports/confusion_matrix.png`. Each run retains independent copies through the
Tracking Server artifact proxy. Before comparison tests, back up existing outputs
outside the repository. After testing, restore the baseline model (max_depth=10)
and verify it matches Git HEAD; test variants are not implicitly adopted.

Syntax and local environment behavior were checked against installed MLflow 3.16.1
and the [official Projects documentation](https://mlflow.org/docs/latest/ml/projects).
