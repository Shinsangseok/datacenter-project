# DEV write-free 추론 검증

이 기능은 승인된 DEV 운영자가 Pod 내부 Unix Domain Socket(UDS)을 통해 이미 로드된 모델을 검증하기 위한 선택 기능이다. 기본값은 비활성이며, 공개 `backend.main:app`에는 검증 라우트나 mount가 없다. 이 소스 변경만으로 현재 배포나 GitOps 설정이 활성화되지는 않는다.

## 기존 API와의 관계

[공통 예측 함수](../backend/inference.py)는 기존 순서대로 네 가지 feature의 한 행 DataFrame을 만들고 `model.predict()`, label 변환, `model.predict_proba()`를 수행한다. 기존 [`POST /predict`](../backend/main.py)는 계산만 위임한다. 응답의 다섯 필드, 원본 확률 DB INSERT, 트랜잭션 commit, commit 이후 Counter/Gauge 갱신, DB 실패의 503 응답은 유지한다. 공통 함수에서 발생한 모델 오류는 기존 공개 API의 오류 처리 규칙을 따른다.

[별도 verifier](../backend/write_free_verifier.py)는 같은 메모리 모델을 사용한다. HTTP 요청마다 DB 연결·조회·쓰기, Registry 조회·다운로드, 모델 재로딩, Dify/외부 HTTP 호출, 업무 메트릭 변경, 일반 파일 쓰기를 하지 않는다. 기존 앱 startup의 DB 읽기와 모델 로딩, UDS 생성/정리, CPU·프로세스 자원 변화는 이 요청별 계약과 구분한다.

## 활성화와 접근 경계

다음 조건을 모두 만족해야 시작한다.

- `WRITE_FREE_INFERENCE_ENABLED`가 정확히 `true`이며 `POD_NAMESPACE=datacenter-app-dev`, `MLFLOW_ENV=dev`, `MODEL_SOURCE=registry`이다. 플래그 미지정 또는 `false`는 비활성이다. 잘못된 값이나 활성 상태의 PROD 설정은 startup 실패로 처리한다.
- 기존 [Registry loader](mlflow/inference-loader.md)와 DB Startup Gate가 성공했고, 준비 상태가 `True`이다. 별도 서버는 DB/Registry credential을 받지 않고 이 기존 gate의 완료 상태를 신뢰한다.
- 검토된 `RandomForestClassifier`의 정확한 타입, feature 순서와 `[0, 1]` class 계약을 만족한다. local 모델이나 임의 subclass는 verifier에서 거부한다. 요청 중 모델 identity나 준비 상태가 바뀌어도 거부한다.
- 단일 프로세스·worker이며 reload를 사용하지 않는다. worker 관련 환경변수와 실행 인자, multiprocessing parent를 검사하고 고정 소켓의 중복 소유를 거부한다.
- 실제 effective UID가 `10001`이다. `/tmp/datacenter-write-free`를 새로 만들고 소유 UID와 `0700`을 확인한다. 그 안의 `inference.sock`을 AF_UNIX로 bind하고 소유 UID와 `0600`을 확인한 다음 listen한다.

기존 디렉터리, stale socket, symlink, 잘못된 소유자·권한을 자동 복구하거나 덮어쓰지 않는다. 종료 시 자신이 만든 inode만 정리한다. 환경변수로 소켓 위치나 UID를 바꾸는 기능은 없다. TCP listener, Service, Ingress, 공개 OpenAPI, proxy, 인증 토큰 또는 CORS 경로를 추가하지 않는다.

Namespace/feature 환경변수는 오배포 방지 조건이며 사용자 인증 수단은 아니다. 호출 권한은 기존 Kubernetes 운영자 인증·인가와 Pod exec 접근, Unix 파일 권한에 의존한다. 다른 UID는 접근할 수 없지만 같은 UID와 root를 격리하는 sandbox는 아니다. 현재 운영자의 DEV/PROD exec 권한을 이 구현이 축소하지 않는다.

후속 DEV Manifest 검토에서 namespace와 feature opt-in의 정확한 Downward API 출처, 승인된 Pod·Deployment UID·imageID·Worker, 단일 프로세스, 소켓 경로의 hostPath/PVC/shared-volume 부재를 별도로 확인해야 한다. 코드만으로 Pod spec이나 운영자 권한을 입증할 수 없다. Secret/RBAC/NetworkPolicy 변경은 이 소스 구현에 포함하지 않는다.

## UDS HTTP 계약과 한도

전용 앱의 유일한 업무 라우트는 `POST /verify/predict`이다. 입력은 다음 네 JSON 숫자만 허용한다. 문자열 숫자, boolean, NaN/Infinity, 누락·추가 필드는 거부한다.

```json
{"cpu": 35.0, "memory": 60.0, "temperature": 45.0, "power": 350.0}
```

범위는 CPU/memory 0–100, temperature -50–150, power 0–10000이다. 응답은 다음 네 필드뿐이며 저장 ID, server ID, timestamp 또는 모델 원시 배열을 반환하지 않는다.

```json
{"mode": "write_free", "persisted": false, "prediction": "NORMAL", "probability": 0.12}
```

확률은 기존 API와 같은 다섯 자리 반올림을 사용한다. 원본 출력의 shape, 유효 label, 유한한 두 class 확률과 합을 추가 검증하며 비정상 출력은 고정 메시지의 503으로 거부한다. 검증 오류는 입력을 반사하지 않는 422이다.

- Startup 완료 후 프로세스당 최대 600초·10개 HTTP 요청이다. 잘못된 라우트·입력·거부 요청도 parsing 전에 계수하며 초기화 API는 없다.
- 실제 누적 body를 4KiB로 제한하고 전체 body 수신 deadline은 2초이다. Content-Length만 신뢰하지 않는다.
- 동시에 verifier 계산 하나만 허용하며 초과 요청은 429이다. 모델 계산은 별도 thread에서 수행하지만 동일 모델을 재로딩하지 않는다.
- HTTP 취소나 client timeout 이후에도 실제 계산이 끝날 때까지 슬롯을 유지한다. 종료도 진행 중인 계산을 기다린다. Python thread에서 진행 중인 native 계산을 안전하게 강제 중단할 수 없으므로 계산 시간의 강제 상한이나 hard kill 시 graceful drain을 보장하지 않는다.
- 예산 소진은 새 검증 요청만 거부한다. 공개 앱의 health/readiness나 기존 `/predict`를 실패시키거나 재시작을 유도하지 않는다. verifier의 예산을 갱신하기 위한 Pod 재시작은 별도 운영 승인 대상이다.

보조 서버는 공개 Uvicorn의 신호 처리와 logging 설정을 바꾸지 않는다. verifier access log와 TRACE scope logging을 사용하지 않으며 요청 payload·인증 header·모델 예외 내용을 오류 응답에 넣지 않는다.

## 검증 범위

[기존 CI 테스트 모듈](../tests/test_fastapi_startup.py)에 기존 테스트를 보존하면서 다음 검증을 추가한다.

1. 공통 계산과 기존 `/predict`의 응답·INSERT·commit·메트릭 순서 및 오류 회귀.
2. DEV/PROD·모델·Startup Gate·프로세스·소켓 fail-closed, 공개 route/OpenAPI 격리.
3. 실제 synthetic RandomForest와 실제 UDS HTTP, 취소·본문·예산·동시성·종료 검증.
4. 요청별 DB·외부 HTTP·모델 다운로드·업무 메트릭·일반 파일 쓰기 호출이 0회임을 trap으로 확인.

이식 가능한 unit test의 UID/path fixture와 실제 OS 권한 검증을 구분한다. 별도의 오프라인 증거에서 UID 10001의 HTTP 성공, UID 10002의 PermissionError, 디렉터리 0700/소켓 0600, 실제 SIGTERM 전달·정리를 확인한다. 이 오프라인 검증은 운영 Registry artifact·실제 Kubernetes exec·후보 이미지의 실배포 성공 증거가 아니다.

같은 프로세스에는 기존 쓰기 가능한 DB engine이 있다. Write-free는 검토한 모델·요청 경로와 회귀 테스트로 입증하는 애플리케이션 계약이며, 별도 DB 계정이나 OS sandbox가 강제하는 read-only 격리가 아니다. 악성 artifact, 같은 UID/root 침해, 프로세스 내 코드 실행을 방어한다고 주장하지 않는다. 기존 Dify HTTP 인증·Legacy LoadBalancer·이미지/의존성 고정 관련 보안 WARN은 유지한다.

## 새 이미지와 후속 승인

기존 GHCR 후보 digest의 내용은 불변이므로 이 새 경로를 포함할 수 없다. 소스 검토·회귀 검증 뒤 별도 승인된 PR/병합과 새 이미지 build/push·provenance/digest 검증이 필요하다. 이 단계에서는 requirements, Dockerfile, 모델 artifact, CI, GitOps, PROD 설정을 바꾸지 않는다. 실제 이미지에 포함된 Python/Uvicorn 의존성이 오프라인 검증 환경과 일치하는지도 확인해야 한다.

후속 순서는 소스 PR 검토 → 승인된 새 이미지 생성·검증 → 제한된 DEV GitOps 변경 검토 → 별도 배포 승인 → 고정 Pod/Worker/이미지 식별과 Startup Gate 확인 → 승인된 exec/UDS HTTP 검증 → 별도 승인된 원본 이미지 Rollback 검증이다. 기존 GitOps safety validator를 포괄적으로 완화하지 않는다. 기존 후보·원본 Rollback digest, 캐시·UID 확인, Auto Sync/Prune/SelfHeal 비활성 조건과 PROD 무변경 조건을 그대로 보존한다.

기존 후보 이미지를 유지하면 현재의 mock/정적/health 검증은 가능하지만 이 UDS 기능을 사용할 수 없다. `/predict`를 호출하거나 모델을 별도로 실행하는 우회로를 이 경로의 검증으로 대체하지 않는다. 운영 DB 쓰기, Migration, Argo CD Sync, 이미지 build/push/pull, 배포·Rollback은 각각 승인된 후속 단계에서만 수행한다.
