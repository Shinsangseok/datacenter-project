# PROD Auth promotion / rollback 계획 — 실행하지 않음

이 문서와 `k8s/overlays/prod`는 현재 공통 Auth 코드 구조의 **staged candidate**다. 코드·로컬 tests/render PASS와 실제 DEV E2E/PROD 준비 완료는 다르다. 이번 단계에서는 PROD DB/account/Secret 생성·변경, migration/bootstrap, Auth enable, API rollout, writer freeze, HPA pin을 하지 않는다. 기존 한 RDS만 사용한다.

## 확정된 target 및 아직 남은 gate

- PROD Tracking: 기존 `mlflow`, 59 tables/revision `b7e2c1a4d9f3` 유지. 새 Tracking schema setup을 하지 않는다.
- 신규 dedicated PROD Auth DB: **`mlflow_auth_prod_v1`**. 공통 environment policy, PROD policy, Secret contract, runbook 모두 같은 이름이다.
- 기존 `mlflow_auth` 3-table partial 및 `mlflow_auth_test` lab은 보존 대상이다. 새 DB로 자동 인정·재사용·복원하지 않는다. DEV DB/account도 변경하지 않는다.
- PROD Auth target 준비, exact user@host/Secret validity, backup/Secret/PVC recovery, [새 DEV E2E](dev-auth-revalidation.md), PROD 전환·rollback rehearsal은 아직 gate다.
- 외부 writer는 사용자가 **현재 확인 불가**라고 답했다. 미확인 상태에서는 freeze 완료 또는 promotion readiness PASS를 선언하지 않는다.
- 현재 MLflow는 1 replica/Recreate다. Auth OFF→ON 중 Ready가 떨어지는 직접 적용을 무중단으로 표현하지 않는다. 동일 PVC의 candidate 접근/자원, 기존 anonymous consumer 전환과 service cutover를 DEV에서 검증하기 전에는 live cutover 금지다. HPA를 수동 고정하여 우회하지 않는다.

## DB 및 계정 준비 계획 (실행 전 별도 승인)

| 역할 | 계획 | 최소 권한 / 평상시 상태 |
|---|---|---|
| Auth migration | 예정 신규 identity `mlflow_auth_migration_prod` + 승인 host restriction | 해당 Auth DB의 SELECT/INSERT/UPDATE/CREATE/ALTER/INDEX/REFERENCES만 실행 직전 일시 부여; 평상시 LOCKED/USAGE only |
| Auth app/bootstrap | 예정 신규 identity `mlflow_auth_app_prod` + 승인 host restriction | 해당 Auth DB SELECT/INSERT/UPDATE/DELETE; DDL/global/GRANT OPTION 없음; REQUIRE SSL |
| Tracking runtime | 현재 승인된 기존 PROD Tracking identity 보존 | 기존 Tracking DML만, Auth/DEV 권한 추가 없음 |
| FastAPI DB | 기존 PROD connection/identity 보존 | 이번 Auth 준비 단계에서 권한·계정 변경하지 않음 |

예정 username은 생성 지시나 기존 계정의 rename 대상이 아니다. 실행 단계 전에 `mysql.user`, grants, role inheritance, user@host 충돌/기존 용도/DEFINER/session을 read-only 검증한다. 이미 존재하면 임의 재사용·ALTER하지 않고 중단/검토한다. 실제 host restriction/password는 private provisioning 입력으로 확정한다. 현재 DEV 5계정과 기존 PROD/partial/lab 계정의 naming cleanup은 계속 보류한다.

신규 DB 준비 승인 후에만 같은 RDS/server UUID에 `utf8mb4 / utf8mb4_unicode_ci` dedicated schema를 준비한다. migration 전 schema 존재, table/view/routine/trigger/event 모두0, revision row 없음, approved charset/collation, 다른 환경 접근 DENY, TLS CA+hostname 검증, migration LOCKED 및 executor/session/lock owner0을 확인한다. **DB 미존재는 clean baseline PASS가 아니다.** 기존 object가 하나라도 있으면 자동 drop/retry하지 않는다.

Migration account와 runtime account의 password/host restriction/TLS를 분리 보존한다. schema grant wildcard escape/role inheritance를 실제 MySQL 설정에 맞게 검사한다. 위 권한 외에 pinned migration이 요구하는 권한이 발견되면 broad grant 대신 중단/검토한다. migration 직전에만 unlock하며 성공/실패 모두 terminal Job, residual session0, lock 해제, LOCK/revoke/USAGE only를 확인한다. ACCOUNT LOCK만으로 기존 session이 사라졌다고 판단하지 않는다.

## 값 없는 Secret contract

정확한 기계 판독 계약은 [secret-contracts.json](../../k8s/overlays/prod/secret-contracts.json)이다. 실제 Secret을 생성하지 않으며 DEV 값을 복사하지 않는다.

| namespace/name | required key | 사용 범위 |
|---|---|---|
| mlflow/mlflow-auth-migration-db | connection.json: host/port/database/username/password | migration Job만 |
| mlflow/mlflow-auth-runtime-db | 같은 JSON fields | Auth server, bootstrap |
| mlflow/mlflow-auth-server | MLFLOW_FLASK_SERVER_SECRET_KEY, MLFLOW_AUTH_ADMIN_USERNAME | server; bootstrap은 username만 |
| mlflow/mlflow-auth-bootstrap | MLFLOW_AUTH_ADMIN_PASSWORD | bootstrap Job만 |
| mlflow/mlflow-target-policy | MLFLOW_EXPECTED_DB_HOST/USER, MLFLOW_EXPECTED_AUTH_HOST/MIGRATION_USER/RUNTIME_USER, MLFLOW_EXPECTED_SERVER_UUID | 독립 승인 대상; 연결 Secret을 자기 승인 입력으로 사용 금지 |
| datacenter-app/datacenter-mlflow-client | MLFLOW_TRACKING_USERNAME, MLFLOW_TRACKING_PASSWORD | non-admin PROD client, model READ + experiment2 READ |
| datacenter-app/datacenter-target-policy | DB_EXPECTED_HOST, DB_EXPECTED_USER, DB_EXPECTED_NAME | 기존 PROD application DB 독립 승인 |

기존 `mlflow-runtime-db`, `datacenter-api-secret`, `dify-api-secret`은 유지한다. key 누락을 optional/ref fallback으로 우회하지 않는다. 값은 Git/image layer/log/evidence에 저장하지 않고 Secret ref/UID/resourceVersion/key completeness만 기록한다. migration/bootstrap Pod에는 automountServiceAccountToken=false; 다른 workload가 해당 Secret을 mount/get할 수 없는지 RBAC와 namespace write 권한도 별도 검토한다. 종료 후 Job/session뿐 아니라 Secret 접근도 제한한다.

## 현재 live와 candidate render 차이

2026-09-29 read-only inventory 기준이다. 실제 실행 직전 다시 확인한다.

| 대상 | 현재 live | 준비한 candidate / 보존 항목 |
|---|---|---|
| FastAPI image | 1.5-mlflow-registry-20260918 | DEV 후보 `git-a647d80@sha256:7f8e22800304d641930283c95be2b5d34325af8fab72f79b3851ff445e6b646e` |
| API 인증/DB | MLflow credential ref 없음, implicit PROD 설정 | client Secret ref, explicit MLFLOW_ENV=prod, independent DB expectations, RDS CA mount |
| API model | 고정 Registry V1 | 그대로 유지, local/DEV fallback 금지 |
| API Dify | 기존 private config/Secret 사용 | Secret ref 보존. repo의 빈 DIFY_BASE_URL은 live 값이 아님. private local input을 별도 검토하여 유지해야 함 |
| API/HPA | datacenter-app/datacenter-api, HPA min2/max3/CPU20%; 관찰 시3 Ready | 같은 이름/selector/service/resources/HPA. base replicas2는 HPA 초기값이며 live current replica를 덮어쓸 값이 아님 |
| MLflow image/Auth | upstream full 3.16.1 / Auth OFF | reviewed digest `sha256:076a69736001ea886c9ce06d8bda3d6626ea85d3dcfaf66c933598d817754115`, 공통 runtime guard/READ authorizer |
| MLflow target | Tracking mlflow | Tracking 유지 + dedicated Auth mlflow_auth_prod_v1 |
| MLflow storage/service | mlflow/mlflow-artifacts PVC, ClusterIP, 1 replica/Recreate | 이름/namespace/PVC/mount/Service/resources/strategy 유지. 전환 가용성은 별도 gate |
| 추가 resources | PROD Auth Job/policy/credential 없음 | 해시 이름의 code/launcher/policy/CA ConfigMap, suspended migration/bootstrap Job, Secret reference만 |

raw render를 전체 apply하면 Auth·API가 동시에 전환되고 빈 private config/replica 값을 덮어쓸 수 있다. **전체 overlay apply 금지**. private config review 후 단계별 승인된 template/config 변경만 추출해야 한다. 실제 rollout patch에는 HPA 소유 replica를 포함하지 않는다. Secret 값이나 기존 mutable ConfigMap을 덮어쓰지 않고 versioned reference를 준비하여 이전 참조를 복구 가능하게 보존한다.

## Writer freeze 설계

현재 live Service는 FastAPI LoadBalancer(8080→8000 및 node port), MLflow ClusterIP이며 해당 namespace에 Ingress controller route가 없다. API에는 write-maintenance flag가 없다. 따라서 존재하지 않는 ingress pause 명령이나 Pod Ready만으로 freeze를 주장하지 않는다. 다음 중지 수단은 계획이며 이번에 실행하지 않았다.

| 대상 | 무엇을 쓰는가 | 승인 후 중지 방법 | drain 증거 | 재개 조건 |
|---|---|---|---|---|
| datacenter-sim/datacenter-simulator | POST /predict → PROD app measurements, 6개 synthetic server/10초 주기 | 원래 replicas 기록 후 해당 Deployment만0으로 축소. in-flight HTTP 완료 여부 별도 확인 | simulator Pod 종료/새 요청0, API 처리 중 요청0, 관련 DB transaction0, 마지막 row watermark 이후 안정 | E2E·observation 및 owner 승인 뒤 기록한 replica 복원 |
| PROD FastAPI write traffic | /predict INSERT; /analysis/run은 외부 Dify 실행 가능 | 모든 caller/대시보드/test의 신규 POST를 발신 측에서 quiesce. public LB/node-port, Service, Pod direct, port-forward 경로의 호출자를 모두 확인. caller 불명 또는 강제 차단 수단 미검증이면 STOP | 단순 sleep 대신 in-flight request0와 DB transaction0, row count+기존 hash 안정, Dify 실행 종료. client timeout은 DB commit 종료 증거가 아님 | Auth client/새 Pod model load/DB smoke/rollback 준비 확인 후 호출자별 재개. HPA/Ready 유지 |
| datacenter-api-registry-test | PROD config/DB credential 사용, 호출되면 /predict write 가능; Registry는 read consumer | 요청자를 먼저 quiesce하고 현재 template/ref/replica 기록 후 해당 test Deployment0 계획 | test Pod 종료·요청0·DB session/transaction0, main API에 우회 요청 없는지 확인 | 기존/new Auth credential 호환성 검증 후 원래 replica/caller 복원 |
| datacenter-api-auth-test | DB는 auth_test_app, Registry URI는 PROD; 주로 Auth 전환 영향을 받는 consumer | PROD app writer라고 단정하지 않음. caller/target 재확인 후 별도 test owner가 요청을 quiesce. lab 변경 승인 없이 scale/DB 수정하지 않음 | startup/load의 PROD Registry 의존성과 활성 요청을 확인 | 새 Auth credential 호환 검증 전 무조건 재개 금지 |
| train_model.py / MLproject / 수동 Registry UI·script | runs/params/metrics/tags/artifacts, 등록 script는 Registry metadata | repo training 경로는 확인됨. 실제 host/external 실행 여부는 미확인. 소유자가 scheduler/manual 실행을 중지하고 실행 중 run 종료·upload drain 확인 | Job/cron/process 및 외부 owner 확인, Tracking row/hash·artifact inventory 안정. DB session0만으로 미실행을 증명하지 않음 | READ-only client로 training write를 재개할 수 없음. writer가 실제 필요하면 별도 권한/운영 설계 승인 전 promotion STOP; admin credential 배포로 우회 금지 |

PROD namespaces의 Job/CronJob writer는 조회 범위에서 없었다. 이것은 외부 writer 부재 증거가 아니다. 사용자의 외부 writer 상태는 **UNCONFIRMED**이므로 freeze 설계의 실행 gate는 닫혀 있다. 승인 단계에서 owner/정확한 호출 경로/중지·재개 방법을 채우고 모두 서명해야 한다. 필요하면 write 유입 gate를 별도로 구현·DEV 검증해야 하며 이번 Auth 공통화에 임의 추가하지 않았다.

순서: caller/scheduler 신규 실행 중지 → simulator 및 승인된 test writer 중지 → in-flight 요청/upload drain → DB transaction/session·row/hash/artifact 안정 확인 → freeze checkpoint. 최소 관찰 구간은 실제 최대 요청/분석 timeout보다 길어야 하며 긴 처리 작업은 명시적 완료 증거가 필요하다. FastAPI/HPA를0 또는 min=max로 고정하지 않는다.

## 승인 후 promotion 순서

1. 이번 commit의 DEV E2E/rollback PASS, frozen release evidence, stable PROD Ready/health/restart, target/account/Secret completeness와 writer owner gate를 확인한다.
2. 위 writer freeze/drain 절차를 수행하고 원래 상태/시각/checkpoint를 기록한다. 현재 미확정 외부 writer가 남으면 여기로 진입하지 않는다.
3. Auth 신규 DB의 clean baseline 및 server/TLS/권한/lock을 read-only 재확인한다. 기존 데이터가 있다면 진행하지 않고 별도 Auth backup/restore 검토한다.
4. Tracking backup의 exact S3 VersionId/SSE-KMS/CMK/checksum/isolated restore 증거를 현재 freeze checkpoint와 비교한다. 9/23 검증된 Tracking backup은 존재하지만 최신 cutover checkpoint로 자동 인정하지 않는다. Auth/PVC artifact/grants/Secret recovery는 별도다. dump에 없는 데이터를 복구했다고 주장하지 않는다.
5. 정확히 승인된 migration identity에 필요한 Auth schema grants만 일시 부여하고 직전에 unlock한다. pinned image/migration hashes/import 구조/독립 target 정책, executor0을 확인한다.
6. 공통 fenced migration 경로를1회 실행한다. suspend 해제는 해당 승인 Job만, parallelism/completions1, backoff0/Never/deadline300, 같은 물리 session lock, reconnect0. target mismatch/unknown checkpoint/fencing 오류면 중단; 자동 retry 금지.
7. Job 정상 종료 후12 tables/revision f1a2b3c4d5e6/independent v2 schema·migration data fingerprint 확인. residual session0/lock 해제 후 즉시 migration 계정 LOCK/revoke/USAGE only, Secret 접근 제한. 실패 때도 cleanup은 필수다.
8. 모든 검증 PASS에서만 DML-only runtime identity와 별도 PROD bootstrap Secret으로 공식 bootstrap1회. admin/user/role/permission 초기 상태 확인, Job 종료, schema/revision/fingerprint 불변. retry하지 않는다. migration 계정 상태를 다시 확인한다.
9. DEV 검증된 공통 코드/image/config로 Auth candidate를 검증하고 공식 관리 경로로 별도 non-admin PROD client를 준비한다. model READ+experiment2 READ만 부여한다. no/invalid401, READ200, write/admin403을 확인한다. canonical Service를 anonymous consumer에게 먼저 전환하지 않는다.
10. 기존 API/test consumer의 client credential 준비와 Service 전환 순서를 검증된 별도 cutover 절차대로 수행한다. 현재 Recreate1 replica manifest만으로 무중단이 보장되지 않는다. 기존 consumer의 새 Pod도 모델을 읽을 수 있어야 하며 이 전환 gate가 미해결이면 Auth enable 이전에 STOP한다.
11. 승인된 PROD FastAPI client Secret/config 참조와 **DEV에서 재검증된 동일 digest**로 template rollout. HPA/PROD DB/기존 Secret 변경 금지, 별도 PROD image build 금지. raw overlay apply 금지.
12. /health·/ready200, authenticated Registry V1 READY, PROD artifact source, model.skops checksum `4ca847e70707f560b88fe280f9abea570b29a643b2219e8f0dba7d0719c4b308`, 승인된 synthetic inference1건의 PROD app write를 확인한다. 이 write는 미래 promotion 단계이며 이번 작업에서는 하지 않았다. 기존 row 집합·Tracking/Auth·artifact·DEV 격리 불변도 확인한다.
13. 최소10분 및 합의된 observation 동안 Ready/새 restart/자원/HPA/인증·권한/로그·DB를 확인한다. writer freeze를 유지한 상태로 PASS 후 owner별 원래 설정을 복원한다. 외부 training writer를 broad/admin으로 재개하지 않는다.

## 단계별 rollback

배포 직전 live Deployment **template**, Service selector, image manifest digest+runtime ID, ConfigMap checksum/내용의 안전한 복구 경로, Secret ref/UID/RV 및 승인된 별도 credential recovery, PVC/model/DB checkpoint를 보존한다. Secret 실제 값은 evidence에 넣지 않는다. base yaml은 live private config의 백업이 아니다.

| 단계 | application/config 복귀 | DB/Secret 보존 및 freeze |
|---|---|---|
| A: migration 전 | 아직 바꾸지 않은 기존 서비스 유지. 준비한 새 참조만 되돌림 | DB 복원/downgrade 없음. 이미 freeze했다면 기존 정상 기능/target·executor0 확인 후 원래 writer 복원 |
| B: migration 후 Auth 전 | executor 종료/cleanup, 기존 Auth OFF 서비스 유지 | 신규 Auth schema는 결과를 보존·격리. 자동 drop/downgrade 없음. LOCK/USAGE/residual0, 기존 서비스 정상 확인 후 재개 |
| C: Auth 후 API 전 | 승인된 이전 MLflow template/Service 경로와 consumer-compatible credential refs 복귀 | Auth bootstrap/client 데이터 보존. Auth OFF로의 복귀는 사전 합의된 보안 경계 안에서만. Registry/model/새 Pod startup 정상까지 freeze |
| D: API rollout 실패 | 진행 중인 rollout을 중단하고 검증된 이전 image/config/Secret refs 조합 복원. Auth ON 호환성이 입증되지 않은 구형 API면 C의 MLflow 복귀도 필요 | Auth/Tracking/PVC와 smoke row 보존. HPA replica를 manifest로 덮어쓰지 않음. health/model/DB·관찰 PASS 전까지 freeze |
| E: writers 재개 후 | 다시 caller freeze/drain, 새 writes의 범위/checkpoint를 고정한 뒤 호환되는 application/config 복귀 | 최신 정상 데이터 보존/reconcile. 오래된 backup을 live에 덮어쓰지 않음. 손상이 입증된 경우에만 별도 승인된 isolated exact-version restore 검증 후 복구; 완료까지 freeze |

`rollout undo`만으로 ConfigMap/Secret/Auth DB/Service 복원이 완료됐다고 판단하지 않는다. DB downgrade는 자동 수행하지 않는다. residual migration session이 남으면 LOCK만 확인하고 진행하지 말고 승인된 recovery로 전환한다. 모든 복구는 기존 image/config/Secret 참조와 실제 credential validity/TLS, schema/model checksum, DEV 격리, owner unfreeze 확인까지 포함한다.
