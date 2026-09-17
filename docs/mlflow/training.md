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

The input, feature order, stratified 80:20 split and RandomForest parameters are
unchanged. Local outputs remain `models/model.pkl`, `reports/metrics.json` and
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
