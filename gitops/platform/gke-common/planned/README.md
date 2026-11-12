# Planned GKE services — deployment HOLD

These are selected, unchanged resource documents from the validated GKE-03A draft. They are local review artifacts. No deployment, sync, Secret creation, IAM change, data restoration or public exposure is authorized by this directory.

The root builds `bootstrap/` only. Airflow, DEV Service and Gateway each have a separate build root and are not pulled into that root. Applying a zero-replica profile can still create PVCs, networking or billable resources. Do not apply this directory to the cluster.

- `bootstrap/`: future platform, Airflow and DEV namespaces, existing draft ServiceAccounts, unbound observer Roles, and unchanged deny/DNS/opt-in metadata NetworkPolicies. Existing Argo and monitoring namespaces are excluded. The draft DNS policy is not proof of compatibility with the current GKE DNS configuration.
- `airflow-migration-held/`: three zero-replica Deployments, an internal Service and two empty restore-target PVCs. Metadata DB, preserved cryptographic material, DAG/plugins/logs, executor/KPO identity and single scheduler ownership remain unverified.
- `dev-service-held/`: internal DEV Service only. The GKE-02A DEV Deployment is a separate build root.
- `dev-platform-prep/`: existing validated GKE-03A combination of that DEV Deployment and Service, with its original replicas=0/RuntimeDefault patch. This is the held candidate compatible with the planned restricted namespace. Do not combine it again with the independent DEV or Service roots.
- `gateway-held/`: Gateway and future Airflow/DEV routes, health checks and GFE ingress policies only. Domains end in `.invalid`; certificate, supported Gateway APIs/class, exposure/authentication and cost approval are unresolved. No routes for the already installed Argo/Grafana services are included.

The existing `gke-common-retain-balanced` StorageClass is owned by the actual monitoring foundation and is referenced, not recreated here. Retain is not a backup. New empty PVCs are not data-migration evidence.

No GCP MLflow runtime draft was part of the GKE-03A common-services implementation. No MLflow Deployment, namespace, KSA, PVC, auth migration/bootstrap Job or replacement image is invented here. Its custom-image/data/identity prerequisites remain in [the integration document](../../../../docs/gcp/dev-and-planned-services.md).

See [DEV and planned service gates](../../../../docs/gcp/dev-and-planned-services.md) before any later execution approval. No Application/ApplicationSet, sync policy, Secret value or service-account key is included.
