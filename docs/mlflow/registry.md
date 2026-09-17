# Baseline Model Registry

Registered Model: `datacenter-anomaly-detector`

Version **1** is READY. It contains the existing baseline RandomForestClassifier:
max_depth=10, n_estimators=100, class_weight=balanced, random_state=42, n_jobs=2.
No retraining was performed.

## Purpose and lineage

Experiments and training runs record parameters, evaluation metrics and artifacts.
Registry supplies a stable model name and explicit versions for review and future
approved deployment. Registration is not production promotion.

| Lineage | Value |
| --- | --- |
| Source training run | `da557a8ef1634c0880d93751691972cf` |
| Original artifact | `models/model.pkl` on the source training run |
| Training Git commit | `73c5d50ce9f15bbbe89275ca5c70e8fab621463d` |
| Packaging run | `7bbbe828c5e3444d97454176e69b1027` |
| Packaging artifact | `baseline-model/` on the packaging run |
| Packaging Git commit | `355268f793379205e1584d85ef77a8d7b4386b4c` |
| Explicit Registry URI | `models:/datacenter-anomaly-detector/1` |

The FINISHED training run was not resumed or modified. A separate FINISHED run,
tagged `purpose=registry-packaging`, holds the MLflow flavor package. It records
`source_training_run_id`, `synthetic_data=true`, `baseline=true` and Git provenance.
No training metrics or hyperparameters were copied to the packaging run.

Version 1's source points to that packaging artifact, and its run_id points to the
packaging run. Version tags also include the source training run ID, packaging run
ID, baseline/synthetic markers, Git commits, serialization format and trusted type.
Accuracy, precision, recall and F1 remain on the source training run; follow the
lineage instead of treating duplicate version tags as another metric authority.

## Serialization and trust

The source joblib artifact was downloaded and byte-compared with the repository
baseline. The source model was confirmed to be a project-trained RandomForest.
It was then packaged separately using:

```python
serialization_format="skops"
skops_trusted_types=["sklearn.tree._tree.Tree"]
```

Actual skops inspection reported exactly `sklearn.tree._tree.Tree` as untrusted.
This is the installed scikit-learn 1.9.0 internal tree storage type. The exception
is limited to this reviewed, provenance-verified model; it is not a general grant
of trust to externally supplied tree models. No blanket trust, pickle or cloudpickle
serialization fallback was used. The original joblib artifact remains unchanged.

Skops provides explicit type inspection and trust configuration. Saving a proper
MLflow Model adds flavor, environment and input-signature metadata for standard
Registry loading without altering the production model format.

## Verification and access

With `MLFLOW_TRACKING_URI` set externally to the reachable Tracking Server:

```python
import os
import mlflow
import mlflow.sklearn

mlflow.set_tracking_uri(os.environ["MLFLOW_TRACKING_URI"])
model = mlflow.sklearn.load_model("models:/datacenter-anomaly-detector/1")
```

Version 1 reloaded successfully as RandomForestClassifier. All parameters matched
the baseline; 6,000 predictions were identical. Probabilities matched with
rtol=1e-12 and atol=1e-12 (observed maximum absolute difference about 1.39e-17).
The source run's metadata and artifact listing were unchanged, and its original
model artifact still matched the repository baseline byte for byte.

## Production boundary

This model uses synthetic data. Its evaluation does not establish real incident
performance. FastAPI continues loading `models/model.pkl`; Dockerfile, backend
code, Kubernetes and the repository model were not changed. FastAPI readiness
remains separate from Registry verification.

No aliases, automatic staging/production promotion, latest-version loading or
model deployment were added. Future promotion/deployment requires explicit
approval and independent operational validation.
