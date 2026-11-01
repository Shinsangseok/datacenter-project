# Explicit FastAPI model source

The default `MODEL_SOURCE=local` keeps the existing `joblib.load(models/model.pkl)`
behavior and API contract. The file is still copied into the image. Registry
loading is opt-in; this change does not switch the running deployment.

Example local-mode settings for review; this is not a deployment instruction:

```text
MODEL_SOURCE=local
MLFLOW_MODEL_NAME=datacenter-anomaly-detector
MLFLOW_MODEL_VERSION=1
MLFLOW_ENV=dev
```

Set `MODEL_SOURCE=registry` only when explicitly selecting Registry mode. This
implementation permits only this model name, numeric version 1 and the internal
Service URL that matches `MLFLOW_ENV=dev` or `prod`. DEV requires its explicit
approved URI; the implicit PROD path exists only for legacy compatibility.
Other versions, aliases and latest selection are rejected. A later version or
endpoint requires an explicit code/configuration review. Credential references
are supplied separately through the environment-selected client component;
never copy DEV credentials into PROD.

`backend/model_loader.py` retrieves Registry metadata and the fixed model URI
through HTTP. It requires a READY version with a `mlflow-artifacts:/` source;
FastAPI downloads through the Tracking Server proxy, without PVC mounts or MLflow
backend DB credentials. Temporary model files are removed after loading.

Before deserialization the loader checks the sklearn flavor, skops format,
scikit-learn 1.9.0 metadata, the exact trusted-type list, the expected file name,
absence of custom code and actual skops untrusted types. Only
`sklearn.tree._tree.Tree` is trusted, through a code constant rather than arbitrary
artifact metadata. This trust applies to the reviewed baseline only.

The raw sklearn model is loaded with `skops.io.load` after validation, preserving
both predict and predict_proba. It must be a RandomForestClassifier with feature
order `cpu, memory, temperature, power` and classes `[0, 1]`. Existing endpoint
logic keeps 0=NORMAL, 1=ANOMALY, and probability column 1 as anomaly probability.

There is no fallback from registry to local. Failed lookup, download, validation
or loading raises ModelLoadError during application import, before the FastAPI app
can start and become Ready. Exceptions at this boundary have sanitized messages.
A successfully loaded model stays in memory; readiness does not continuously
query Registry. Existing database readiness behavior is unchanged.

The image installs `requirements-inference.txt`, which includes the existing
pinned requirements plus `mlflow-skinny==3.16.1`, `skops==0.15.0`, and
`PyYAML==6.0.3`. It does not install training requirements or full MLflow. Transitive dependencies still need
image validation; a clean Python environment check is not a container build.

Before rollout, validate the rebuilt image as UID 10001, writable temporary
storage, memory/startup timing, HTTP connectivity and probes. Test Registry outage
and invalid metadata startup failures in an isolated test deployment. Keep local
mode explicit for initial rollout and enable registry only after operator review.
The current integration adds environment-separated Auth/client manifests and
offline validation, while retaining main's startup, retry, readiness and API
contracts. These source files have not been applied. The historical candidate
image digests in the manifests do not prove that this integration commit is
built or deployed. No live ConfigMap, Secret, Registry or production model was
changed. See [the integration review](integration-review.md) and
[DEV revalidation gates](dev-auth-revalidation.md) before any rollout.
