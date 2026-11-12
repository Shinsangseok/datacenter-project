GKE Standard DEV FastAPI Overlay — GKE-02A 오프라인 초안

상태와 범위
이 경로는 독립 소스 사본에만 작성했다. Kubernetes 적용, Helm 설치, Argo Sync,
Git commit/push/merge, DB/Artifact 변경은 승인되지 않았고 실행하지 않았다.
원본 저장소 기준 HEAD: 2801343e67452e5d5170e341cc77e7717908ec4f.
GKE_02A_MANIFEST_READY는 이 초안의 오프라인 검토 가능 상태이며 배포 승인이 아니다.

렌더 계약
- kubectl kustomize gitops/overlays/gcp/dev
- 공통 gitops/apps/fastapi/base를 직접 상속한다. AWS overlay를 상속하지 않는다.
- 출력은 datacenter-app-dev/Deployment/datacenter-api 1개다.
- Namespace, ServiceAccount, Service, ConfigMap, Secret, NetworkPolicy, PVC는
  이 Deployment 전용 app이 생성하지 않는다. 플랫폼/서비스 소유자가 별도 승인으로 준비한다.
- replicas=1/Recreate, 기존 progressDeadlineSeconds=600 및 history=10을 유지한다.
- 승인 후보 image=ghcr.io/shinsangseok/datacenter-api@sha256:5358f946c6c4bc853af2687577257821699870ef5e3e9a8d148c68cecadd773c.
  앱 source=001aeaeb93cc80312e0de1890ecb46c7b4baca16, imagePullPolicy=IfNotPresent.
  사용자 제공 기존 GKE Pull/실행 PASS 및 CD-12 AWS 실증을 재사용한다.
- namespace 이름은 backend/write_free_verifier.py의 DEV guard와 일치해야 한다.
- AWS hostname/datacenter.io selector와 AWS dedicated toleration은 이식하지 않는다.
  cloud.google.com/gke-nodepool=shared-pool은 배치 초안이다. 현재 노드0 상태에서
  실제 node label/taint/용량을 검증했다고 주장하지 않는다. 노드 수는 변경하지 않는다.

필수 외부 의존성 — 이름/key만 선언하며 실제 값은 포함하지 않음
모든 참조는 datacenter-app-dev namespace 안에서 준비해야 한다.
1) ServiceAccount/datacenter-api-dev
   automountServiceAccountToken=false는 공통 base에서 유지한다.
   초기 API는 MySQL/MLflow 내부 HTTP를 사용하므로 GCP IAM 역할을 자동 부여하지 않는다.
   실제 Google API 필요 시 이 KSA용 최소 WIF 권한을 별도 설계한다. 노드 SA의 image
   pull identity, KSA Google API 권한, Kubernetes RBAC는 별개의 경계다.
2) ConfigMap/datacenter-api-config
   필수 key DB_NAME. 승인된 DEV 논리 DB datacenter_app_dev와 일치해야 한다.
3) Secret/datacenter-api-secret
   필수 keys DB_HOST, DB_USER, DB_PASSWORD. GKE DEV 격리 대상의 자격을 별도 보안
   공급 경로로 준비한다. AWS 운영 Secret을 복사하거나 보고서에 값을 출력하지 않는다.
4) Secret/datacenter-target-policy
   필수 keys DB_EXPECTED_HOST, DB_EXPECTED_USER, DB_EXPECTED_NAME.
   마지막 key는 이 초안의 명시적 정책 의존성이다. AWS envFrom config에서의 암묵
   상속을 사용하지 않는다. 실제 접속 자격으로 자기 승인하지 않고 독립 승인한
   host/user/name과 대조한다. DB_EXPECTED_NAME은 datacenter_app_dev여야 한다.
5) Secret/datacenter-mlflow-client
   필수 keys MLFLOW_TRACKING_USERNAME, MLFLOW_TRACKING_PASSWORD. DEV read client
   계정으로 준비한다. Tracking/Auth DB 자격을 FastAPI에 mount하지 않는다.
6) ConfigMap/datacenter-api-db-ca
   필수 key ca.pem. 대상 DB의 검증된 공개 CA PEM을 별도 승인으로 공급한다.
   volume db-ca에 key를 ca.pem으로 투영하고 /etc/datacenter/certs를 readOnly mount.
   DB_SSL_CA=/etc/datacenter/certs/ca.pem, DB_PORT=3306을 명시한다.
   AWS mlflow-rds-ca-75fggf29d8 hash나 AWS 호스트 경로를 복제하지 않는다.
   Cloud SQL proxy/connector를 쓸 경우 현재 DB host/TLS guard와의 호환성을 새로
   설계해야 하며 이 초안이 그 경로까지 지원한다고 가정하지 않는다.
7) Service/datacenter-api는 외부 소유. DEV 권장 형태는 ClusterIP, port8080→8000,
   selector app=datacenter-api. 이 overlay는 Service/LB/Ingress/공개 UDS를 만들지 않는다.

envFrom을 사용하지 않고 필요한 DB/MLflow keys를 explicit env로 지정했다.
Dify/외부 HTTP/임의 AWS config key를 자동 상속하지 않는다. 기존 분석 API가 Dify를
필요로 하는 전체 기능은 별도 설정·인증·실행 승인 전 초기 준비 범위에 포함하지 않는다.
MLFLOW_ENV=dev, MODEL_SOURCE=registry,
MLFLOW_TRACKING_URI=http://mlflow.mlflow-dev.svc.cluster.local:5000을 고정했다.
backend/model_loader.py의 exact URI/model Version1 계약 때문에 AWS 외부 URL로
env만 변경해 MLflow 없이 단독 기동하는 우회는 허용하지 않는다.
DEV MLflow Tracking/Auth/Artifact 및 모델V1 READY/URI/hash/TLS가 선행 Gate다.
FastAPI 자체 PVC는 필요하지 않으며 MLflow Artifact PVC와 구분한다.

Probe 개선 초안
startupProbe: GET /startup:8000, period5s/timeout2s/failureThreshold48/successThreshold1.
명목 240s는 측정용 초안이며 실제 cold-start, 중단시간, 정확한 재시작/종료 deadline의
보증 또는 운영 승인이 아니다. 같은 source의 Policy B 앱 예산120s는 in-flight native
작업을 강제 중단하지 않으며 Kubernetes probe와 terminationGracePeriod30s도 실제
관찰이 필요하다. 노드0인 이번 작업에서는 지연·메모리·CPU·probe 실행을 실측하지 않았다.

/startup은 승인 이미지 source001aeaeb93cc80312e0de1890ecb46c7b4baca16의
backend/main.py:156-161에 실제 존재하며 startup_complete/model latch만 검사한다.
원본 Git에서 git show <해당 SHA>:backend/main.py로 읽기 확인했다.
보존된 CD_12_AWS_FINAL_REVIEW_REPORT.txt:17도 동일 후보 Startup200을 기록한다.
/ready는 DB SELECT1을 수행하므로 초기화 latch 전용인 /startup과 구분한다.
기존 liveness /health 및 readiness /ready의 설정은 base 그대로 유지한다.
docs/examples/fastapi-dev-startup-probe.patch.yaml은 NOT APPLIED이며 threshold48이
승인되지 않은 측정후보라고 명시한다. 이 초안 작성은 그 값을 운영 승인하지 않는다.
배포 전에 격리 DEV cold-start/장애/중단예산을 검토해 이 값의 사용 여부를 승인한다.

보안·자원 유지
Pod UID/GID10001, runAsNonRoot=true, automount=false, 컨테이너 privilege escalation
금지/drop ALL, requests100m/256Mi, limits1CPU/1Gi를 base와 동일하게 유지했다.
기존 미명시 seccomp/readOnlyRootFilesystem을 검증 없이 PASS로 바꾸지 않는다.
Recreate/replica1의 중단 가능성을 유지하며 무중단 배포라고 표현하지 않는다.
WRITE_FREE_INFERENCE_ENABLED=false로 초기 준비와 UDS 실행 단계를 분리했다.
이는 공개 /predict의 DB 쓰기 기능을 비활성화하는 설정이 아니다. 초기 접근과
writer 소유자는 RBAC/NetworkPolicy/호출자 통제로 별도 제한해야 한다.

남은 Gate
- Namespace/KSA/ConfigMap/Secret/CA/Service/NetworkPolicy 준비와 실제 key/대상 검증.
- DEV DB schema/data/TLS/독립 target-policy, DEV MLflow 일관된 restore/model 준비.
- GKE 노드/배치·image/custom MLflow 가용성·권한·프로브 실동작은 별도 승인 범위.
- 실제 UDS 요청별 DB/외부HTTP/업무metric mutator 무쓰기 독립 계측은 UNVERIFIED.
  AWS UDS HTTP200와 Argo rollback PASS를 동일 요청계측 PASS로 확대하지 않는다.
- MLflow Tracking/Auth/Artifact 백업·복원, AWS R01~05 WARN/NOT_GRANTED,
  독립 Kubernetes 비상복구 UNVERIFIED와 원본 AWS 보존 조건은 그대로 유지한다.
- Airflow metadata/암호복구/DAG/logs·PVC 이관과 scheduler 단일 writer는 후속 별도 범위.
- DEV 초기 배포 준비와 PROD 전체 데이터 이전·cutover·AWS 정리 승인은 구분한다.
