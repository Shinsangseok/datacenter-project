# DEV read-only client and service isolation

DEV uses the official MLflow Auth user/role APIs. Its non-admin client receives a
role in the default workspace with exactly `registered_model` /
`datacenter-anomaly-detector` / `READ` and `experiment` / `2` / `READ`.
Keep the existing client Secret password; no database account naming changes.
No migration or administrator bootstrap is needed to register a client.

The reviewed MLflow build can permit resource creation when workspaces are off.
For this DEV read-only service, `read_only_auth:authenticate` first delegates to
MLflow Basic authentication, then denies non-admin methods except GET/HEAD.
MLflow's resource authorization still runs for reads, including its native
FastAPI artifact bridge. This also denies client password/profile mutation and
POST-based searches; those are outside this read-only client contract. The
configured bootstrap admin can use the official management API. PROD keeps its
existing authorization configuration.

`runtime_guard.py` replaces the deployment-only bootstrap snapshot/count gate.
The strict one-time bootstrap verifier remains unchanged. Startup compares all
12 table structures and the expected revision to `expected-schema-v2.json`,
independently derived from the reviewed expected DDL fixture. This includes
effective charset/collation, types/defaults/nullability/indexes/FKs/constraints.
It rejects extra schema objects, missing/demoted bootstrap admin, another admin,
empty identities/password hashes, and duplicate identities. Ordinary non-admin
clients and managed role/permission data do not require a new expected row count.
Migration/checkpoint data and allocation-counter comparisons are unchanged.
The target policy must contain the independently approved server UUID.

`k8s/overlays/dev/network_policy.py` renders egress-only NetworkPolicies for
`mlflow-dev` and `datacenter-app-dev`. It allows CoreDNS UDP/TCP 53 (namespace AND
pod selectors), those DEV namespaces on TCP 5000/8000, and independently verified
RDS IPv4 addresses on TCP 3306. All other egress is denied. No Kubernetes API
access is needed by these workloads. No PROD namespace policy is created.

Before applying, verify the CNI/controller enforcement, approved RDS hostname,
DNS A records, and cluster Pod/Service/node ranges. Pass resolved RDS addresses
and prohibited cluster ranges to `policies()`; it rejects overlap. Review and
store the rendered result outside Git, then apply only these two DEV policies.
The policies are not included as static overlay resources because actual RDS
addresses must not be committed or guessed. RDS address changes require a
reviewed policy refresh; absent refresh the connection fails closed. DB-level
DEV/PROD isolation continues to rely on MySQL grants on the shared RDS endpoint.
NetworkPolicy is additive: separately review future egress policies for bypasses.
The host network is outside this pod egress boundary.

Validate from DEV pods: DNS, DEV health, RDS TLS, authenticated Registry/artifact
reads; deny PROD Service and Pod addresses and deny client writes/admin calls.
Use the existing expected model checksum. Observe PROD availability/restarts and
compare protected DB/Registry/Kubernetes state. Do not deploy DEV FastAPI or
enable PROD Auth as part of this change.
