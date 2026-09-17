# MLflow Tracking Server (3.16.1)

## Deployment

`k8s/mlflow/` is a Kustomize deployment, independent of the FastAPI image and its
requirements. `requirements-mlflow.txt` provides the local MLflow/training environment.

- Namespace/Deployment/Service: `mlflow`; one replica, one worker, `Recreate`.
- Official `v3.16.1-full` image pinned by digest in `resources.yaml`.
- ClusterIP port 5000; no Ingress, NodePort or LoadBalancer.
- Runtime-only RDS MySQL backend, schema `mlflow`, TLS with CA and hostname validation.
- PVC `mlflow-artifacts`: 10Gi, RWO, `local-path`, mounted at `/mlflow/artifacts`.
- Requests: 250m / 512Mi; limits: 1 CPU / 1Gi; non-root UID/GID 10001.

The namespace needs two externally supplied resources before the Pod starts:

| Resource | Required keys |
| --- | --- |
| Secret `mlflow-runtime-db` | `DB_HOST`, `DB_USER`, `DB_PASSWORD` (runtime only) |
| ConfigMap `mlflow-rds-ca` | `global-bundle.pem` (public RDS CA bundle) |

Provision the Secret from the protected external credential file and the existing
DB host setting using an in-memory Kubernetes JSON object passed to kubectl stdin.
Do not write its rendered YAML, URI, endpoint or credentials into this repository,
command arguments, logs or shell history. Never inject the existing application's
administrator Secret or migration credentials into this Pod. The CA ConfigMap is
provided from the existing external CA bundle, not downloaded at Pod startup.

Review changes with `kubectl diff -k k8s/mlflow`. On first deployment, if the
namespace is absent, review `kubectl kustomize k8s/mlflow` and use client dry-run
before creating the namespace and externally supplied resources. Then apply:

```sh
kubectl apply -k k8s/mlflow
kubectl rollout status deployment/mlflow -n mlflow
```

`launcher.py` constructs the SQLAlchemy URL in memory and passes it via
`MLFLOW_BACKEND_STORE_URI`, never argv. Child output is filtered for credentials,
encoded credentials and MySQL URIs before reaching container logs. Environment
variables remain available to privileged process/container administrators.
Startup checks require schema revision `b7e2c1a4d9f3` and 59 tables before invoking
MLflow. The launcher does not run migrations. A future MLflow upgrade requires a
separate migration procedure and review of these guards; do not grant DDL to runtime.

## Access and artifact storage

Internal URI: `http://mlflow.mlflow.svc.cluster.local:5000`.
For local access, use an authenticated Kubernetes connection:

```sh
kubectl port-forward -n mlflow service/mlflow 5000:5000 --address 127.0.0.1
```

Open `http://127.0.0.1:5000`. Allowed hosts include the Service DNS variants and
localhost on port 5000. CORS origins are explicitly limited to local port 5000.
This deployment does not add MLflow user authentication; ClusterIP is internal
exposure, not an authorization boundary.

Artifacts use `--serve-artifacts --artifacts-destination /mlflow/artifacts` and
`--default-artifact-root mlflow-artifacts:/`. Clients use HTTP upload/download and
need no PVC access. The initial persistence smoke test is explicitly tagged
`purpose=deployment-smoke-test`, `production=false`.

The local-path volume is tied to this node. Its StorageClass uses Delete reclaim
policy and does not allow volume expansion. Preserve/back up artifacts before
removing the PVC/namespace or replacing the node. For later S3 migration, copy
existing artifacts with their relative paths intact and verify reads before
changing the proxy destination; changing the destination does not migrate files.

## Verification

Check Pod readiness, PVC binding, `/health`, UI, and create one explicitly named
test experiment/run. Verify metadata in RDS, artifact bytes on PVC, HTTP download,
and both after replacing the Pod. Inspect logs and process arguments through a
redacting checker, rather than blindly printing raw output on connection errors.
FastAPI `/ready` and monitoring readiness are checked separately without Dify or
Bedrock requests. No application training code is changed by this deployment.
