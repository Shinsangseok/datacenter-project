# DEV/PROD Auth 공통 구성

이 문서는 로컬 코드·manifest 준비 상태를 설명한다. PROD Auth는 아직 live에서 OFF이며, 이 변경은 apply/migration/bootstrap/Secret 생성 승인이 아니다. 공통 코드 변경 후 [DEV 재검증](dev-auth-revalidation.md)이 완료되기 전에는 새 release를 PASS로 판정하지 않는다.

## 같은 코드, 환경별 policy

Auth의 모든 진입점은 `auth_config.load()`를 사용한다. `MLFLOW_ENV`를 명시하고, 함께 배포하는 [environment-targets.json](../../k8s/mlflow/environment-targets.json)의 환경별 승인 DB와 실행 policy/connection Secret을 비교한다. 환경별 Python 구현은 없다.

| 환경 | namespace (Tracking/Auth) | Tracking DB | Auth DB |
|---|---|---|---|
| dev | mlflow-dev | mlflow_tracking_dev | mlflow_auth_dev |
| prod | mlflow | mlflow | **mlflow_auth_prod_v1** |

PROD Auth DB 이름은 `mlflow_auth_prod_v1` 하나로 통일한다. 기존 `mlflow_auth`(3-table partial), lab `mlflow_auth_test`, 다른 환경 DB는 policy와 Secret 양쪽을 같은 잘못된 이름으로 바꿔도 거부한다. 새 PROD DB는 아직 생성하지 않는다. 환경 contract 파일 자체는 검토된 release artifact이며 live schema에 맞춰 자동 생성하지 않는다.

Auth는 양쪽 환경에서 독립 `MLFLOW_EXPECTED_AUTH_HOST/USER`와 승인 server UUID를 요구한다. UUID는 `MLFLOW_EXPECTED_SERVER_UUID`로 주입하며 기존 concrete policy UUID가 있으면 서로 일치해야 한다. 실제 host/user/password/UUID는 Git에 넣지 않는다. expected 값을 connection Secret에서 자동 추론하지 않는다. TLS-off, 다른 포트, system DB, unknown environment, placeholder identity, 정책 충돌은 시작 실패다.

Tracking은 같은 정책의 `tracking_database`와 `MLFLOW_EXPECTED_DB_NAME/HOST/USER`를 대조한다. Auth와 Tracking은 같은 기존 RDS host를 쓰되 DB/account는 다르다. 기존 비인증 `launcher.py`의 implicit PROD 호환 경로만 남아 있다. **Auth는 implicit environment를 허용하지 않으며 두 overlay 모두 명시적 설정이다.** 호환 launcher를 새 Auth 배포 경로로 사용하지 않는다.

## 반드시 통과하는 server startup 경로

`runtime_guard.main → auth_server.main → prepare → auth_config.load / tracking_connection → verify_runtime → verify_tracking → basic-auth server`.

`auth_server.py`를 직접 실행해도 동일 `verify_runtime`을 거친다. guard를 별도 wrapper에만 두어 우회할 수 있었던 구조를 없앴다. 다음 검사는 DEV/PROD에 동일하다.

- 읽기 전용 SELECT/SHOW preflight: 실제 DB, server UUID, CURRENT_USER, CURRENT_ROLE=NONE, TLS cipher.
- 12 base tables, 추가 view/routine/trigger/event 없음, revision `f1a2b3c4d5e6`.
- 독립 fixture의 v2 fingerprint: effective charset/collation, type/default/nullable/index/FK/constraint. expected hash를 live 값으로 승인하지 않는다.
- 지정 bootstrap admin 1명, 유효하고 유일한 user identity/password hash. 일반 client 수는 제한하지 않는다.
- Tracking revision `b7e2c1a4d9f3`, 59 tables, TLS, 실제 DB 검증.
- bootstrap admin password는 server에 금지. config/child log의 raw/URL-encoded credential을 마스킹한다.

두 환경 모두 `default_permission=NO_PERMISSIONS`와 `read_only_auth:authenticate`를 사용한다. 인증 실패는401, non-admin의 허용 목록 밖 요청/쓰기/admin 경로는403이다. 허용된 GET/HEAD도 MLflow의 model/experiment/artifact resource 권한 검사를 통과해야 한다. admin만 공식 관리 API를 사용할 수 있다. DEV-only authorizer 우회 분기는 없다.

Migration fencing과 bootstrap 본체, revision hash, v2 canonicalization/expected fixture는 변경하지 않았다. 같은 물리 session의 advisory lock, reconnect 금지, unknown checkpoint 거부, migration hash/head 검증을 보존한다. migration target preflight에서 server identity를 독립 확인해야 하며 runtime UUID 검증을 migration 실행 증거로 대신하지 않는다.

## 같은 manifest 구조

- `k8s/components/mlflow-auth`: 공통 server patch, migration/bootstrap Job, Auth code ConfigMap.
- `k8s/components/mlflow-client`: 공통 FastAPI credential/TLS wiring과 동일 image digest.
- `k8s/overlays/{dev,prod}/mlflow`: 환경 DB/mode/Service allowlist와 Auth policy.
- `k8s/overlays/{dev,prod}/api`: 환경 URI/DB 및 replica·Dify 차이. PROD HPA/Service/PVC/selector/replica/resource는 기존 base를 상속한다.

세 Auth 진입점은 `PYTHONPATH=/opt/mlflow:/opt/mlflow/auth`를 쓴다. `db_target.py`와 `environment-targets.json`은 각각 `/opt/mlflow/`의 명시적 `subPath`로 mount하고 Auth 코드는 `/opt/mlflow/auth`에 둔다. ConfigMap timestamp symlink의 parent를 추론하지 않는다. subPath 갱신에는 새 Pod가 필요하다.

migration/bootstrap은 모두 suspended, backoffLimit0, restartPolicyNever, parallelism/completions1, deadline300초다. render에는 실제 Secret object가 없다. Job을 포함한 overlay 전체를 한 번에 apply하지 않는다. 기존 DB가 migration 완료 상태인 DEV에서는 migration/bootstrap을 다시 실행하지 않는다.

FastAPI는 `MODEL_SOURCE=registry`, model `datacenter-anomaly-detector`, Version1 고정이다. Auth 실패/접근 불가/다른 version에서는 local 또는 다른 환경 fallback이 없다. DEV Dify는 비활성, PROD 기존 Dify 설정/Secret은 유지해야 한다. repo의 빈 Dify URL을 live 값 위에 apply하지 않는다.

## Image와 code/config 증거

| artifact | 양쪽 overlay의 동일 후보 digest |
|---|---|
| FastAPI (`git-a647d80`) | `sha256:7f8e22800304d641930283c95be2b5d34325af8fab72f79b3851ff445e6b646e` |
| reviewed MLflow/Auth 3.16.1 | `sha256:076a69736001ea886c9ce06d8bda3d6626ea85d3dcfaf66c933598d817754115` |

API Python 코드와 image bytes는 이번에 바꾸지 않았다. Auth 코드가 ConfigMap으로 mount되므로 image digest만으로 이번 변경의 검증을 주장하지 않는다. 다음 DEV 재검증에서 **새 Git commit + image digest + 코드/정책 ConfigMap checksum + render checksum**을 함께 고정한다. PROD용 별도 build는 없다.

`k8s/overlays/prod`는 이제 Auth 승격 **목표 구성**이다. 기존 Auth-OFF base 복사본이 아니다. 기존 단일 manifest와 `k8s/base`는 과거 호환 경로로 남아 있으며, 실제 rollback에는 배포 직전 live checkpoint가 필요하다. [PROD runbook](prod-promotion-runbook.md)의 준비·전환 gate가 해결되기 전에는 apply하지 않는다.

## Secret 및 계정

[PROD Secret contract](../../k8s/overlays/prod/secret-contracts.json)에 name/key/consumer만 기록한다. `mlflow-target-policy`에 승인 server UUID 키가 추가되므로 DEV 재검증 전에 별도 승인된 DEV 준비가 필요하다. key가 없으면 Pod가 시작되지 않으며 optional/fallback으로 우회하지 않는다.

현재 DEV 계정 5개의 기존 이름/host/password/grants는 그대로 둔다. 향후 naming cleanup 후보는 `mlflow_app_dev`, `mlflow_migration_dev`, `mlflow_auth_app_dev`, `mlflow_auth_migration_dev`, `datacenter_app_dev`이며 이번 작업에서 rename/drop/recreate하지 않는다. PROD Auth 계정 준비 계획은 runbook에 별도로 기록한다. DEV Secret 값을 PROD로 복사하지 않는다.

## 로컬 검사

```sh
MLFLOW_DISABLE_AGENT_HINT=1 .venv/bin/python -m unittest discover -s tests -v
kubectl kustomize k8s/overlays/dev
kubectl kustomize k8s/overlays/prod
git diff --check
```

이 검사들은 live E2E가 아니다. sensitive scan과 별도 승인된 DEV E2E/rollback을 마친 후에만 promotion readiness를 다시 판정한다. 기존 RDS1대 구조, schema init의 DEV-only one-shot 성격, FastAPI startup DDL 금지, backup의 exact VersionId/SSE-KMS/checksum 계약은 유지한다.
