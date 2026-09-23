# DEV/PROD 공통 구성 — 코드 준비 단계

이 변경은 코드와 로컬 Kustomize 렌더링까지만 준비한다. 실제 namespace/DB/account/Secret/PVC 생성, migration, Kubernetes apply, production 변경을 수행하지 않는다. 기존 partial/lab Auth DB 및 저장소 밖 reviewed evidence는 보존한다.

## 범위와 현재 한계

- 기존 PROD: `datacenter-app` / `mlflow`. Deployment/Service/PVC 이름, namespace, selector, image, HPA 설정 유지.
- 새 DEV: `datacenter-app-dev` / `mlflow-dev`.
- 기존 RDS 한 대에 `mlflow_tracking_dev`, `mlflow_auth_dev`, `datacenter_app_dev`를 별도로 준비할 계획이다. 코드가 DB를 생성하지 않는다.
- `backend/main.py`는 변경하지 않았다. 시작 시 measurements CREATE를 수행하므로 DEV app DB/계정을 별도로 준비해야 한다.
- 새 overlay는 **provisioning 및 Auth 배포가 완료된 manifest가 아니다**. NetworkPolicy/Quota/RBAC, 필요한 namespace/CA/Secret, Tracking schema/Registry V1, reviewed Auth image 및 migration/bootstrap Job wiring이 다음 단계에 필요하다.
- 현재 DEV MLflow overlay는 공통 Tracking launcher를 렌더링하며 Auth를 자동 활성화하지 않는다. Auth 소스는 `k8s/mlflow/auth/`에 준비했다. 별도 검토 후 해당 server/migration/bootstrap 진입점과 Secret·policy mount를 연결해야 한다.
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

스크립트 배포 시 `db_target.py`가 `auth/`의 부모 디렉터리에 위치하도록 같은 artifact에 함께 포함한다. 예: `/opt/mlflow/db_target.py`, `/opt/mlflow/auth/auth_server.py`. migration hash 파일도 auth/에 함께 둔다. policy·Secret은 코드 artifact에 bake하지 않는다. 실제 image build/Job mount는 다음 단계이며 stock MLflow image에 reviewed migration patches가 있다고 가정하지 않는다.

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

## 다음 승인된 DEV provisioning 작업

1. PROD 가용성과 공유 노드/RDS 여유 확인. DEV quota/network/RBAC 계획 확정. HPA 수동 고정 금지.
2. DEV namespace 2개 및 현재 RDS 내부의 새 DB 3개를 별도 단계에서 생성. 기존 partial/lab DB 보존.
3. DEV 전용 least-privilege 계정, 승인 target 입력, CA 및 Secret을 안전한 외부 경로에서 준비. 실제 값 Git 기록 금지.
4. reviewed Auth 의존성/patch와 새 launcher/loader 코드를 포함한 artifact를 한 번 빌드하고 digest/hash 기록. Auth Job/server 배포 wiring을 검토하고 동일 artifact로 실행.
5. DEV Tracking schema, artifact PVC, 고정 Registry V1 fixture 준비. DB/계정/서비스의 PROD 접근 차단 확인.
6. 새 DEV Auth empty baseline → fenced migration → head/fingerprint → bootstrap/RBAC → migration lock/session0/revoke.
7. DEV Auth/Registry/artifact/FastAPI 정상·오류 경로 및 backup/rollback 검증. DEV PASS와 수동 승인 전 PROD DB/account/Secret 준비·promotion 금지.

S3 backup은 기존 SSE-KMS/Bucket Key ON, exact VersionId 및 checksum fail-closed를 유지한다. 이번 변경에 AWS resource/backup 실행은 없다.
