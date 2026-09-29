# 공통 Auth 코드의 DEV 재검증 계획 — 아직 미실행

기존 DEV PASS는 이전 ConfigMap 코드에 대한 증거다. 이번 공통화 commit의 live PASS로 재사용하지 않는다. 이 문서는 다음 승인된 DEV 단계의 준비 절차이며, 이번 commit 작업에서는 apply/DB write/Job 실행/image build를 하지 않는다.

## 준비와 변경 범위

1. 기존 DEV Deployment template, image digest, ConfigMap checksum, Secret ref/UID/resourceVersion, PVC, model source/checksum, Auth revision/v2 fingerprint, PROD 보호 baseline을 Git 밖에 보존한다. Secret 값은 저장하지 않는다.
2. PROD/DEV Ready·health, 새 restart 없는 안정성 baseline을 확인한다. 임의 restart나 PROD Ready 저하가 발생하면 DEV 실행을 중단한다.
3. DEV `mlflow-target-policy`의 기존 승인 host/user와 같은 RDS의 승인 `MLFLOW_EXPECTED_SERVER_UUID`를 독립 대조한다. 새 key 준비는 별도 승인된 DEV Secret 변경이다. 계정/password 변경 또는 PROD Secret 접근은 필요 없다.
4. DEV Auth12/revision/v2 fingerprint 및 기존 admin/client 최소 READ 상태, migration 계정 LOCKED/USAGE only/residual0 확인. migration/bootstrap 재실행 금지.
5. 같은 후보 image digest를 유지하면서 **공통 code/target-policy ConfigMap + DEV Auth Deployment에 필요한 변경만** 검토한다. 전체 overlay apply, suspended Job 재생성/해제, 기존 PVC 교체는 하지 않는다. FastAPI image나 application code는 이번에 변경하지 않았으며 기존 authenticated FastAPI로 통합 검사한다.
6. 코드/정책 subPath는 live 자동 갱신이 아니므로 검토된 새 Pod rollout으로만 검증한다. 새 ConfigMap의 hash 이름과 mount가 실제 원하는 commit을 가리키는지 확인한다.

## 필수 결과

| 검사 | PASS 조건 |
|---|---|
| no credential | Registry/model version/artifact 모두401 |
| invalid credential | 동일 요청401, 값/log 노출 없음 |
| 정상 client | model V1·experiment2 READ200, non-admin·최소 role/permission 유지 |
| write/admin | model/experiment 생성·수정 및 user/role 관리403. 예상 외2xx면 더 이상 쓰기 probe를 보내지 않고 중단 |
| Registry | datacenter-anomaly-detector Version1 READY, DEV Registry metadata/source만 사용 |
| artifact | MLmodel/model.skops200; skops SHA-256 `4ca847e70707f560b88fe280f9abea570b29a643b2219e8f0dba7d0719c4b308` |
| FastAPI | /health·/ready200, 인증된 DEV model load, 같은 checksum |
| inference | 식별 가능한 synthetic NORMAL/ANOMALY 요청만; 결과가 datacenter_app_dev에만 기록 |
| PROD 보호 | 원래 PROD row 집합의 hash 유지; simulator append는 별도 구분. DEV marker0. Tracking/Auth/artifact/config 불변 |
| 격리 | DEV→DEV MLflow/DB PASS, DEV→PROD Service/Pod/DB DENY |
| fail-closed | 격리된 DEV probe에서 invalid credential/unreachable DEV MLflow/wrong version 실패; local/PROD fallback0. 정상 Deployment 설정은 유지하거나 즉시 원복 |
| Auth schema |12 tables/revision f1a2b3c4d5e6 및 independent v2 fingerprint 유지; migration LOCKED/USAGE only/residual0 |
| rollback smoke | 공통화 전 검증된 Auth image/config/ref로 DEV application-layer 복귀→Ready/auth/Registry/checksum→이번 동일 digest/config 재승격→동일 smoke PASS |

rollback 중에도 Auth/Tracking schema downgrade, account 변경, migration/bootstrap 재실행은 하지 않는다. 기존 1→0→1 API rehearsal만으로 이번 **Auth config 변경의** rollback PASS를 대신하지 않는다. Secret에 추가된 UUID key는 이전 code와 호환되는 추가 key이지만 실제 이전 ref/password validity도 확인해야 한다.

## 승격 증거

image digest 두 개, 이미지 build commit과 이번 코드/config commit, mounted code/policy checksum, rendered checksum, model checksum, Auth head/12-table v2 map, HTTP401/403/200, DEV DB test row IDs, PROD 보호 비교, 새 restart, rollback/re-promotion 결과를 기록한다. Secret 값/실제 private endpoint는 Git/evidence에 넣지 않는다.

DEV 재검증 PASS 후에만 같은 digest와 같은 공통 code ConfigMap을 PROD 후보로 확정한다. PROD 재빌드나 PROD-only Python fork는 없다. 이 계획의 offline tests PASS는 실제 DEV E2E PASS가 아니다.
