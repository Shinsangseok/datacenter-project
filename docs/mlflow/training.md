# RandomForest training with MLflow

Install `requirements-mlflow.txt` in the training environment. From the repository
root, set `MLFLOW_TRACKING_URI` to the reachable Tracking Server and run:

```sh
.venv/bin/python train_model.py
```

For host training, keep `kubectl port-forward -n mlflow service/mlflow
5000:5000 --address 127.0.0.1` running separately and set
`MLFLOW_TRACKING_URI=http://127.0.0.1:5000`. The code contains no server address.
A future in-cluster client can use the Service DNS without changing training code.
No training Job is installed by this change.

The experiment is `datacenter-anomaly-detection`. Each invocation creates one run;
a missing tracking URI fails before training rather than silently using a local
store. Importing the module does not start training or create a run.

Without arguments, the input, feature order, stratified 80:20 split and
RandomForest parameters are unchanged. Local outputs remain `models/model.pkl`, `reports/metrics.json` and
`reports/confusion_matrix.png`. Every run uploads these same three files under
`models/` and `reports/` within its own artifact directory.

Parameters include model settings, split fraction, ordered features, positive
label and total row count. Metrics include accuracy, precision, recall, F1 and
all four confusion-matrix counts. Tags identify synthetic data, dataset path and
SHA-256 of the exact bytes parsed, Git commit/dirty status and Python/sklearn/MLflow
versions. Git fields are `unknown` if Git metadata is unavailable.

Local files are overwritten on each successful training, as before; back them up
before validation. Earlier MLflow runs retain their independent artifacts. A
tracking/upload error fails the invocation; a failed run may still have local
outputs. Training does not deploy the model to FastAPI, register models, call
Dify/Bedrock, or change Kubernetes resources.

## CLI parameters

| Argument | Default | Accepted values |
| --- | --- | --- |
| `--n-estimators` | 100 | Positive integer |
| `--max-depth` | 10 | Positive integer |
| `--class-weight` | balanced | `balanced`, `balanced_subsample` |
| `--random-state` | 42 | Integer in [0, 2**32 - 1]; shared by split and model |
| `--test-size` | 0.2 | Float strictly between 0 and 1 |
| `--n-jobs` | 2 | Nonzero integer; -1 uses all CPUs, -2 all but one, etc. |

Arguments are validated before contacting MLflow or training. Extremely small or
large test fractions can still fail sklearn's stratified split requirements for
the actual dataset. Feature order, target, positive label, input/output paths and
experiment name remain fixed. MLflow records the actual model/split parameters.

```sh
.venv/bin/python train_model.py --help
.venv/bin/python train_model.py --max-depth 6
```

Both commands use the same environment-based tracking configuration. A training
run with different arguments overwrites the local output files; each MLflow run
keeps its own artifacts. This change does not implement MLflow Projects.
