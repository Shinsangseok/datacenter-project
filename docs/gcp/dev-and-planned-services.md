# GCP DEV 및 후속 서비스 초안

이 문서는 GKE-02A와 GKE-03A의 검증된 파일을 Git 경로로 통합한 범위와 실제 배포 Gate를 설명한다. 이 통합은 클러스터 적용·Argo Sync·Secret 공급·데이터 복원·공개 접속 승인이 아니다. AWS 파일과 공통 FastAPI base는 변경하지 않는다.

## 실제 구축과 계획 구분

| 대상 | 통합 경로 | 상태와 경계 |
| --- | --- | --- |
| Argo CD | [실제 구축 구성](../../gitops/argocd/gcp/) | GKE-03B 구축 결과를 별도 소유한다. 이 planned 경로에 과거 03A Argo 차트·Namespace·AppProject·Application 초안을 중복 포함하지 않는다. |
| Prometheus / Grafana | [실제 구축 구성](../../gitops/platform/gke-common/monitoring/) | GKE-03C 새 빈 저장소 설치와 GKE-03C2 대시보드 1개 구성 이전을 구분한다. 사용자 수동 확인에서 6패널이 표시됐고 각 `No data`였다. 업무 metric 수집과 AWS TSDB 이력·Grafana DB 전체 복원은 완료되지 않았다. |
| DEV FastAPI | [GKE-02A Overlay](../../gitops/overlays/gcp/dev/) | 원본 18/18 정적 검증을 재사용한 Deployment 1개. 이미지와 보안 정책은 그대로이며 실제 앱·DB·모델 배포는 HOLD다. |
| Namespace / KSA / 관찰 Role / NetworkPolicy | [planned bootstrap](../../gitops/platform/gke-common/planned/bootstrap/) | 03A의 platform·Airflow·DEV 객체만 선택했다. Namespace 권한, DNS·WI 경로와 실제 의존성은 적용 전 검토 대상이다. 기존 관측 Namespace와 PROD 대상은 제외했다. |
| Airflow | [planned Airflow](../../gitops/platform/gke-common/planned/airflow-migration-held/) | 기존 03A의 Deployment 3개는 모두 replicas=0이다. Metadata·키·DAG·plugin·로그·executor/KPO 의존성과 쓰기 소유권 확인 전 배포 HOLD다. |
| HTTPS Gateway | [planned Gateway](../../gitops/platform/gke-common/planned/gateway-held/) | 기존 03A 중 DEV/Airflow 경로만 선택했다. `.invalid` 도메인, 신규 TLS Secret 참조와 공개 노출 승인은 미확정이다. 실제 Argo/Grafana용 과거 backend는 제외했다. |
| MLflow | 아래 보존 계약만 문서화 | 03A 공통서비스에는 GCP 런타임 초안이 없었다. AWS용 base를 새 GCP Deployment로 오인해 이식하거나 namespace·KSA·PVC·이미지를 새로 만들지 않는다. 실제 구현과 데이터 복원은 HOLD다. |

현재 공유 노드는 1대이며 과거 02A README의 노드 0대·생성 전 상태는 작성 당시 증거다. 원문을 수정해 과거 검증을 현재 실측으로 바꾸지 않았다. 초기 단일 노드 설계를 유지하며 향후 서비스 요구량은 별도 계측한다.

## 오프라인 build 경계

다음은 로컬 렌더 명령이다. `apply`, 서버 dry-run, Helm 설치, Argo Sync 명령이 아니다.

```bash
kubectl kustomize gitops/overlays/gcp/dev
kubectl kustomize gitops/platform/gke-common/planned
kubectl kustomize gitops/platform/gke-common/planned/dev-platform-prep
kubectl kustomize gitops/platform/gke-common/planned/airflow-migration-held
kubectl kustomize gitops/platform/gke-common/planned/dev-service-held
kubectl kustomize gitops/platform/gke-common/planned/gateway-held
```

`planned` 루트는 bootstrap만 포함한다. Airflow·PVC·Gateway·DEV 앱을 일괄 활성화하지 않는다. `dev-platform-prep`는 DEV Overlay와 Service를 함께 포함하므로 같은 객체를 독립 루트와 중복 조합하지 않는다. 0 replica도 PVC·네트워크·과금 리소스 생성까지 막지 않으므로 적용 금지는 그대로다.

GKE-02A 원본은 replicas=1이고 seccomp를 명시하지 않는다. planned DEV Namespace의 PSS restricted와 함께 활성화할 후보는 03A에서 검증한 `dev-platform-prep`이며, 원래의 replicas=0 및 RuntimeDefault patch만 추가한다. 이 차이를 원본 Overlay에 몰래 반영하지 않는다. 이후 기동에는 별도의 의존성·실행 승인이 필요하다.

## DEV FastAPI 의존성

승인된 이미지 Digest는 `ghcr.io/shinsangseok/datacenter-api@sha256:5358f946c6c4bc853af2687577257821699870ef5e3e9a8d148c68cecadd773c`다. `shared-pool` selector, AWS toleration 제거, UID/GID 10001, automount=false, 권한 상승 금지/drop ALL, requests 100m/256Mi 및 limits 1CPU/1Gi를 원본대로 유지한다. Startup probe의 `/startup`, 5초 간격·48회는 기존 측정 후보이며 런타임 검증·기동시간 보증이 아니다.

다음 이름과 key만 참조한다. 실제 값이나 Secret 리소스는 포함하지 않는다. 모두 `datacenter-app-dev` 범위다.

| 종류 / 이름 | 필요 key 또는 역할 |
| --- | --- |
| KSA `datacenter-api-dev` | token automount=false, 불필요한 GCP IAM 권한 없음 |
| ConfigMap `datacenter-api-config` | `DB_NAME` = 승인된 DEV 논리 DB `datacenter_app_dev` |
| ConfigMap `datacenter-api-db-ca` | 검증된 공개 CA `ca.pem`; 대상 TLS·hostname 검증 필요 |
| Secret `datacenter-api-secret` | `DB_HOST`, `DB_USER`, `DB_PASSWORD` |
| Secret `datacenter-target-policy` | 독립 승인한 `DB_EXPECTED_HOST`, `DB_EXPECTED_USER`, `DB_EXPECTED_NAME` |
| Secret `datacenter-mlflow-client` | `MLFLOW_TRACKING_USERNAME`, `MLFLOW_TRACKING_PASSWORD`; DEV 읽기 client |
| Service `datacenter-api` | planned ClusterIP 8080→8000; 공개 Ingress/LB 없음 |

`envFrom` 대신 필요한 참조만 명시한다. `MODEL_SOURCE=registry`, DEV MLflow 내부 URI와 고정 모델 Version 1의 계약을 유지하며, DB·모델 없이 정상 서비스처럼 보이도록 우회하지 않는다. `WRITE_FREE_INFERENCE_ENABLED=false`는 공개 `/predict`의 업무 DB 쓰기를 차단하지 않는다. `/predict` 호출이나 외부 트래픽 허용은 이번 통합의 검증에 포함하지 않는다.

## 데이터 및 MLflow 보존 Gate

DEV 보존 대상은 `datacenter_app_dev`, `mlflow_tracking_dev`, `mlflow_auth_dev`와 Registry 메타데이터·모델 Artifact다. 소스 MySQL 8.4.9와 격리 Cloud SQL `datacenter-dev-restore` MySQL 8.4.11은 서로 다른 식별자로 검증해야 한다. 인프라 존재나 정적 Manifest 통과는 데이터 복원 성공이 아니다.

Tracking/Registry/Auth/Artifact는 DEV writer 동결·drain 후 같은 논리 checkpoint로 보존하고, 백업·파일 목록/SHA·DB schema/data·모델 버전과 URI·권한을 복원 후 비교해야 한다. 동결 전 Artifact SHA나 테이블 수 1/59/12만으로 전체 카탈로그·trigger·최신 일관성·복원을 PASS 처리하지 않는다. 실제 소스 접근, 전체 객체 가시성, 원본 trigger/DEFINER/실행 의미와 복원 권한, writer의 현재 상태, 별도 복원 검증 및 실행 승인은 계속 Gate다.

기존 운영 custom MLflow 이미지 식별 근거는 `sha256:702214244351fa4cebd784cf8b680e40b5bf9b29582cfa4a335f340d7b3141e1`다. AWS base의 upstream MLflow 이미지나 별도 후보를 이 custom Auth/runtime의 검증된 대체물로 간주하지 않는다. 실제 GKE 배포용 image registry/Digest·공급망·auth guard/CA·Cloud SQL 접속·Artifact 경로 호환성을 추가 구현·검증해야 한다. Auth migration/bootstrap Job을 자동 재실행하거나 원본 DEFINER·URI를 승인 없이 변환하지 않는다.

Cloud Storage DEV Artifact 버킷은 존재하지만 이 초안에는 GCS 전환 설정이나 IAM 변경이 없다. SQL landing 전송과 애플리케이션 Artifact 권한은 분리한다. Workload Identity는 DB 암호·TLS guard·GHCR 인증·Kubernetes RBAC를 대신하지 않는다. KSA/GSA 연결이나 Google API 권한은 필요한 principal과 리소스를 확정한 후 별도 승인한다. 서비스 계정 키 파일은 만들거나 Git에 넣지 않는다.

## Airflow·스토리지·네트워크 Gate

Airflow는 검증된 03A image Digest와 0 replica를 그대로 보존한다. `gke-airflow-database/sql-alchemy-conn`, `gke-airflow-crypto/{fernet-key,jwt-secret}`는 이름/key만 참조한다. 실제 Metadata DB 엔진·이름을 추정하지 않고, 보존된 Fernet/JWT 키·Connections·Users·DAG/plugin과 API/auth 호환성을 검증하기 전 신규 DB·사용자·migration을 만들지 않는다.

두 PVC는 `gke-airflow-dags-restored` 5Gi와 `gke-airflow-logs-restored` 20Gi의 빈 복원 대상이다. 실제 모니터링 소유 `gke-common-retain-balanced`를 참조하며 이 경로에는 StorageClass를 중복 정의하지 않는다. Retain·PVC 이름은 백업이나 복원 증거가 아니다. AWS 파일 UID/GID/mode, 대상 PV/zone/volumeHandle·무결성과 RWO 단일 노드 배치·동시성을 확인해야 한다.

기존 03A default deny·DNS·opt-in metadata NetworkPolicy는 선택된 Namespace에 대해 그대로 보존했다. 현재 GKE DNS/Dataplane·WIF metadata·Cloud SQL/private 경로와 앱/모델 경로가 이 초안으로 통신 가능한지는 검증하지 않았다. 광역 egress나 새 IAM을 추가해 자동 통과시키지 않는다. Gateway의 GFE 허용 CIDR·헬스체크도 오프라인 초안이며 실제 API 지원·인증·DNS·인증서·방화벽·공개 노출 승인이 필요하다.

AWS/GKE 중 Airflow scheduler와 DEV/외부 writer의 소유자는 하나만 활성화해야 한다. DEV만 이동해도 AWS Argo가 DEV worker에 남을 수 있으므로 EC2 종료 근거로 삼지 않는다. Dify 전체 중지·공유 RDS 잠금·PROD 쓰기 중단은 DEV 복원의 승인 범위가 아니다.

## 남은 실행 승인

1. 실제 대상/의존성·현재 writer·소스/복원 권한과 불변 checkpoint 확인.
2. 필요한 최소 실행 환경·SQL landing·IAM 변경을 정확히 분리한 승인.
3. DEV 한정 freeze/backup, 격리 DB·Artifact restore와 trigger/모델/파일 검증 승인.
4. DEV 기동·metrics 수집·관찰·장애복구를 검증한 뒤 writer 재개 승인.
5. PROD·Airflow·Dify·전체 데이터 및 트래픽 전환은 후속 별도 승인. AWS 중지·삭제는 전체 이전 검증 후 별도 승인.

실제 UDS 요청별 무쓰기 독립 계측, 기존 AWS 보안 WARN 및 독립 Kubernetes 비상복구 미검증을 임의 수용하지 않는다. Git 소스 통합이나 대시보드 렌더 확인은 이 Gate를 해소하지 않는다.
