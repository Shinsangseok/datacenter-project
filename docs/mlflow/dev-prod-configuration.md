# DEV/PROD 공통 구성 — 코드 준비 단계

이 변경은 코드와 로컬 Kustomize 렌더링까지만 준비한다. 실제 namespace/DB/account/Secret/PVC 생성, migration, Kubernetes apply, production 변경을 수행하지 않는다. 기존 partial/lab Auth DB 및 저장소 밖 reviewed evidence는 보존한다.

## 범위와 현재 한계

- 기존 PROD: `datacenter-app` / `mlflow`. Deployment/Service/PVC 이름, namespace, selector, image, HPA 설정 유지.
- 새 DEV: `datacenter-app-dev` / `mlflow-dev`.
- DEV 대상은 `mlflow_tracking_dev`, `mlflow_auth_dev`, `datacenter_app_dev`다. 이번 작업은 live 상태를 조회하거나 DB를 생성하지 않는다.
- `backend/main.py` startup은 measurements 컬럼을 SELECT/LIMIT 0으로 검증하며 DDL을 수행하지 않는다. 누락된 schema는 시작 실패로 처리한다. DEV 전용 one-shot 도구는 `backend/initialize_schema.py`로 분리했다.
- overlay는 로컬 구성 준비 상태이며 배포 완료 증거가 아니다. NetworkPolicy/Quota/RBAC, 외부 Secret 및 이미지 가용성, Tracking schema/Registry V1은 별도 운영 검증이 필요하다. RDS 공개 CA ConfigMap과 Auth Job wiring은 DEV에 포함했다.
- DEV MLflow Deployment는 `auth_server.py`를 사용한다. migration/bootstrap Job은 모두 `suspend: true`, `backoffLimit: 0`, `restartPolicy: Never`, parallelism/completions 1, deadline 300초다. 렌더링은 실행이 아니다. 실패 후 자동 retry/reconnect를 추가하지 않았으며 재개는 별도 승인과 checkpoint 검토가 필요하다.
- DEV API의 image tag `dev-candidate-not-built`는 의도적인 미빌드 표시다. 기존 PROD image에는 DEV loader 변경이 없으므로 이를 그대로 DEV에 배포하지 않는다. 다음 단계에서 단 한 번 빌드한 artifact의 digest로 치환한다.
- DEV API는 별도 MLflow client Secret을 요구하고 PROD Dify credential을 참조하지 않는다. PROD Dify 설정/credential은 변경하지 않는다.

## 공통 Tracking 설정

접속값은 `DB_HOST`, `DB_PORT`, `DB_NAME`, `DB_USER`, `DB_PASSWORD`에서만 가져온다. 실제 host/user/password는 외부에서 Secret으로 주입한다. 포트는 현재 승인된 3306만 허용한다.

| 설정 | DEV | 새 명시적 PROD 구성 |
|---|---|---|
| MLFLOW_ENV | dev | prod |
| DB_NAME | mlflow_tracking_dev | 승인된 PROD Tracking DB |
| MLFLOW_EXPECTED_DB_NAME | mlflow_tracking_dev | 승인된 PROD Tracking DB |
| MLFLOW_EXPECTED_DB_HOST | 승인된 RDS host | 승인된 RDS host |
| MLFLOW_EXPECTED_DB_USER | 승인된 DEV Tracking user | 승인된 PROD Tracking user |
| MLFLOW_MYSQL_SSL_CA | 승인된 RDS CA 파일 경로 | 동일 CA 계약 |

접속 Secret과 독립된 target 승인값을 대조한다. 새 환경에서 expected 값을 실제 DB_* 값으로 자동 대체하지 않는다. DEV overlay의 host/user 승인값은 `mlflow-target-policy` Secret 참조만 포함한다. 이는 credential 저장소라기보다 승인 target 입력이며, 실제 private endpoint를 공개 repo에 쓰지 않기 위한 배포 시 주입 계약이다.

기존 PROD에는 MLFLOW_ENV가 없으므로 하위 호환 경로를 유지한다. 이 경로는 기존 `DB_NAME=mlflow` 제한을 유지하며 접속 대상 자체는 DB_*에서 읽는다. 기존에 없던 독립 expected host/user 입력을 갑자기 필수로 만들지 않는다. **이 호환 경로가 새 독립 host/user 검증까지 수행한다고 주장하지 않는다.** 명시적 PROD 구성으로 전환하면 세 expected 입력을 모두 준비해야 한다. DEV에는 이 호환 경로가 없다.

두 환경 모두 `mysql+pymysql`, CA 검증, TLS hostname 검증을 사용한다. server subprocess URI에도 CA를 포함한다. SQLAlchemy가 PyMySQL에 전달하는 `ssl_ca`/`ssl_check_hostname` 조합과 최종 SSLContext의 CERT_REQUIRED·hostname 검증을 네트워크 없는 테스트로 확인한다. DB 연결 후 실제 database, TLS cipher, Tracking revision `b7e2c1a4d9f3`, 59-table gate를 확인한다. SQL은 선택한 DB를 사용하며 production schema 이름을 직접 참조하지 않는다. Python `-O`에서도 새 safety gate가 비활성화되지 않는다.

## Auth 설정 및 reviewed code 보존

`k8s/mlflow/auth/`는 기존 reviewed bundle의 repo 사본이다. 과거 lab 소스를 덮어쓰지 않았다.

- `fenced_migration.py`, `bootstrap_admin.py`, `migration-hashes.json`: reviewed 원본과 동일. 동일 physical session의 GET_LOCK/DDL, reconnect 금지, checkpoint/fingerprint, head/hash 검증 유지.
- `auth_config.py`: production/development 모두 MySQL/TLS. 과거 `isolated-test` 모드와 TLS-off 설정을 거부한다.
- `auth_server.py`: SQLite 분기 제거. 같은 Tracking preflight와 Auth target/TLS/head/admin 검사를 수행한다. 한 RDS라는 승인 구조에 맞춰 Tracking/Auth host는 같고 DB/account는 달라야 한다.
- 정상 server에는 bootstrap admin password를 넣지 않는다. config/child log의 원문 및 URL-encoded credential을 마스킹한다.

Auth 진입점의 환경/파일 계약:

| 입력 | 계약 |
|---|---|
| AUTH_POLICY | 비밀 없는 승인 policy JSON 파일 경로 |
| AUTH_DB_SECRET | connection.json 경로; host/port/database/username/password |
| AUTH_CHECKPOINTS | migration 승인 checkpoint JSON 파일 경로; 최초 empty DB는 [] |
| MLFLOW_ENV | dev 또는 prod |
| MLFLOW_EXPECTED_AUTH_HOST | 승인된 RDS host, 배포 시 Secret/env 주입 |
| MLFLOW_EXPECTED_AUTH_USER | 진입점별 migration 또는 runtime 승인 user |

DEV policy는 `mode=development`, `expected_database=mlflow_auth_dev`, `expected_head=f1a2b3c4d5e6`, `tls_required=true`, 승인된 `ca_file`, `allow_empty=true`를 사용한다. `expected_host`는 비워 두거나 REPLACE_ placeholder로 두고 실제 승인 host를 위 환경변수로 전달할 수 있다. concrete policy host가 있다면 환경변수와 일치해야 한다.

PROD의 기존 `mode=production` policy와 Secret 형식은 계속 지원한다. 새 명시적 `MLFLOW_ENV=prod` 사용 시 Auth expected host/user를 필수로 요구한다. DEV DB 또는 Tracking DB를 Auth 대상으로 선택하면 거부한다.

스크립트 배포 시 `db_target.py`가 `auth/`의 부모 디렉터리에 위치하도록 같은 artifact에 함께 포함한다. 예: `/opt/mlflow/db_target.py`, `/opt/mlflow/auth/auth_server.py`. migration hash 파일도 auth/에 함께 둔다. policy·Secret은 코드 artifact에 bake하지 않는다. DEV server와 Job은 같은 기존 reviewed image digest를 참조하고 Auth code/공통 db_target/policy/CA를 mount한다. 이미지의 실제 가용성은 이번 작업에서 조회하거나 검증하지 않았다. stock MLflow image에 reviewed migration patches가 있다고 가정하지 않는다.

## FastAPI loader

기존 MLFLOW_ENV 미설정 또는 prod는 기존 PROD Service URI와 동작을 유지한다. dev에서는 DEV Service URI를 명시해야 한다. 서비스 namespace는 승인된 prod/dev 이름만 허용한다. 이는 cluster 내부 논리 서비스 계약이며 실제 RDS/Dify endpoint를 코드에 추가하지 않는다.

cross-environment URI, 누락된 DEV URI, 알 수 없는 환경은 Registry lookup 전에 거부한다. 모델 이름/Version 1/skops 검증, artifact proxy 요구, 실패 시 local fallback 금지는 유지한다.

## Kustomize 진입점

```text
k8s/base/api/       기존 API manifest와 동등한 compatibility 사본
k8s/base/mlflow/    기존 k8s/mlflow 진입점 참조
k8s/overlays/dev/   DEV namespace, DB, URI, Secret 참조 patch
k8s/overlays/prod/  기존 base 그대로, Auth enable 없음
```

기존 `k8s/deployment.yaml` 등 단일 파일 사용 경로를 유지하기 위해 API base 사본을 두었다. 두 경로의 drift는 byte equality 테스트로 차단한다. Kustomize 상위 디렉터리 참조 순환을 피하기 위한 한정된 중복이다.

PROD의 이름/namespace/selector/HPA/서비스 유형/replicas를 바꾸지 않는다. 코드가 바뀌므로 generated `mlflow-launcher-<hash>` ConfigMap 이름과 해당 volume 참조는 렌더링 시 새 hash를 가진다. 이것은 적용되지 않은 코드 버전 변경이며 live ConfigMap 변경이 아니다. 최종 PROD deployment 전 별도 검토가 필요하다.

DEV는 API 1 replica, ClusterIP, HPA 미배포다. PROD HPA에는 어떤 patch도 없다. DEV PVC는 새 namespace에 별도로 렌더링된다. 현재 저장소의 용량값을 상속하지만 실제 할당량은 provisioning 전에 검토한다. API namespace는 별도 provisioning 단계에서 준비한다.

로컬 검증만 가능:

```sh
kubectl kustomize k8s/overlays/dev
kubectl kustomize k8s/overlays/prod
MLFLOW_DISABLE_AGENT_HINT=1 .venv/bin/python -m unittest discover -s tests -v
git diff --check
```

## 계정 및 Secret 계약

실제 현재 DEV 계정 5개는 기존 이름을 그대로 유지한다. 이번 작업에 DB account rename/drop/recreate는 없다. 승인 host/user는 접속 Secret에서 자동 추론하지 않고 독립 policy Secret으로 전달한다. 다음 표는 **향후 표준으로 전환이 필요하다는 기록만**이며 현재 계정명 또는 생성 지시가 아니다.

| 역할 | 향후 표준 계정명 |
|---|---|
| Tracking runtime | mlflow_app_dev |
| Tracking migration | mlflow_migration_dev |
| Auth runtime | mlflow_auth_app_dev |
| Auth migration | mlflow_auth_migration_dev |
| API runtime | datacenter_app_dev |

- `mlflow-dev/mlflow-target-policy`: `MLFLOW_EXPECTED_DB_HOST`, `MLFLOW_EXPECTED_DB_USER`, `MLFLOW_EXPECTED_AUTH_HOST`, `MLFLOW_EXPECTED_AUTH_MIGRATION_USER`, `MLFLOW_EXPECTED_AUTH_RUNTIME_USER`. 값에는 승인된 **기존** 계정명을 사용한다. `secret-contracts.json`은 값 없는 계약 문서이며 Secret 생성기가 아니다.
- Tracking 접속: `mlflow-runtime-db`의 DB_HOST/DB_PORT/DB_NAME/DB_USER/DB_PASSWORD.
- Auth 접속: `mlflow-auth-runtime-db`, `mlflow-auth-migration-db` 각각의 `connection.json`에 host/port/database/username/password.
- `mlflow-auth-server`: MLFLOW_AUTH_ADMIN_USERNAME, MLFLOW_FLASK_SERVER_SECRET_KEY. bootstrap password는 별도 `mlflow-auth-bootstrap`의 MLFLOW_AUTH_ADMIN_PASSWORD에만 둔다.
- API 접속: `datacenter-app-dev/datacenter-api-secret`. 독립 승인 입력은 같은 namespace의 `datacenter-target-policy`에 DB_EXPECTED_HOST/DB_EXPECTED_USER로 공급한다. DB_EXPECTED_NAME은 ConfigMap의 `datacenter_app_dev`다.
- API Registry client: `datacenter-mlflow-client`의 MLFLOW_TRACKING_USERNAME/MLFLOW_TRACKING_PASSWORD.

## DEV application schema one-shot 계약

`backend/initialize_schema.py`는 명시적 DEV, 독립 private JSON policy의 expected_host/expected_user/expected_database/tls_required, CA 및 3306 포트를 검증한다. 허용 DB는 datacenter_app_dev뿐이다. 연결 후 실제 DB/TLS와 table/view/routine/trigger/event 상태를 검사한다. 빈 schema에만 기존 measurements DDL을 한 번 실행하고, 기존 measurements만 있으면 SELECT로 컬럼 존재를 확인한다. 컬럼 타입·index의 완전한 schema 비교 도구는 아니다. 알 수 없는 partial schema는 거부한다.

도구의 진입점은 `python -m backend.initialize_schema --policy <private-approved-json>`이다. 이 문서는 실행 승인이 아니며 이번 작업에서는 실행하지 않는다. 승인된 별도 schema 초기화 권한이 필요하고 runtime 계정에 DDL 권한을 추가하지 않는다. NullPool을 사용하며 자동 retry/reconnect는 없다. MySQL DDL 실패 시 rollback으로 복구된다고 가정하지 말고 별도 검토한다.

API도 명시적 MLFLOW_ENV=dev/prod에서는 독립 DB_EXPECTED_HOST/USER/NAME 및 TLS를 요구한다. 기존 MLFLOW_ENV 미설정 PROD의 접속 설정 호환성은 유지하되 DEV DB와 system schema는 거부한다. 기존 PROD가 새 코드를 사용할 경우 schema는 이미 존재해야 한다. PROD manifest 및 live 환경은 변경하지 않는다.

## 후속 운영 작업 — 이번 범위 밖

1. 기존 DEV DB/계정/권한 및 외부 Secret을 별도 승인된 절차로 확인한다. 계정 재생성이나 naming 전환을 선행 조건으로 요구하지 않는다.
2. 미빌드 API artifact와 reviewed Auth image 가용성을 검증한다. 이번 작업에서 image build/deploy는 하지 않는다.
3. Tracking schema/Registry V1 및 필요한 application schema 초기화는 별도 승인 후 수행한다.
4. Auth empty baseline 또는 승인 checkpoint 검토 후에만 migration/bootstrap 실행 여부를 결정한다. Job suspend를 이번 작업에서 해제하지 않는다.
5. DEV 통합 검증, backup/rollback 및 별도 수동 승인 전 PROD promotion은 하지 않는다.

S3 backup은 기존 SSE-KMS/Bucket Key ON, exact VersionId 및 checksum fail-closed를 유지한다. 이번 변경에 AWS resource/backup 실행은 없다.
