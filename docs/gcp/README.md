# GCP migration foundation

AWS DATACENTER-PROJECT를 GKE Standard로 이전하기 위한 검증된 기반 구성을 Git 경로로 통합한다. 현재 완료 범위는 GKE Argo bootstrap, 새 저장소를 사용한 모니터링 기반, AWS Grafana Org1의 대시보드 1개 구성 이전이다. DEV/PROD 업무 서비스·DB·MLflow·Airflow·Dify와 전체 데이터 이전은 완료되지 않았다.

## 문서와 소스

| 문서 | 내용 |
| --- | --- |
| [이전 현황](migration-status.md) | EC2 3대·RDS와 전체 서비스의 완료/미완료, 데이터·보안 Gate 및 후속 로드맵 |
| [통합 기록](integration-notes.md) | worktree·기준 SHA, 출처와 안전한 경로 변환, 버전/Digest, CI 검증 경계 |
| [Argo CD](argocd.md) | 실제 35개 공개 Git 객체와 외부 Secret 3개, 읽기 전용 bootstrap·deny-all Project |
| [모니터링](monitoring.md) | 실제 19개 공개 Git 객체와 외부 Secret 1개, 수집·저장·대시보드 관리 경계 |
| [DEV 및 후속 서비스](dev-and-planned-services.md) | 원본 GKE-02A DEV Overlay와 별도 03A held 후보, Airflow·Gateway 계획 및 MLflow 미구현 Gate |

## 대상과 작업 경계

- Project: `project-05cd81b7-6e8f-4f65-ae7`
- GKE Standard: `datacenter-gke`, `asia-northeast3-a`, `shared-pool`
- 최근 검증 기준: 공유 worker 1대. 전체 AWS 서비스를 수용할 용량·성능·고가용성을 검증한 값은 아니다.
- 독립 작업 branch: `feat/gke-migration-foundation`
- 통합 기준 HEAD: `2801343e67452e5d5170e341cc77e7717908ec4f`

이번 소스 통합 자체는 클라우드 변경이나 기존 설치의 재적용·동기화·권한 확대 승인이 아니다. 원본 AWS Overlay와 FastAPI base는 보존한다. Secret 실값·인증파일·개인 키·token·cookie·운영 DB/Artifact·브라우저 프로필은 Git에 포함하지 않는다.

## 오프라인 검증

저장소 루트에서 다음 명령으로 로컬 렌더와 정적 검증만 수행한다.

```bash
kubectl kustomize gitops/argocd/gcp
kubectl kustomize gitops/platform/gke-common/monitoring
kubectl kustomize gitops/overlays/gcp/dev
python3 scripts/gcp/validate_foundation.py --repo . --output /tmp/gke-foundation-validation.json
```

추가 planned build 루트와 중복 관계는 [통합 기록](integration-notes.md#독립-build-루트)에 있다. 여러 루트의 렌더 결과를 일괄 apply하라는 지침이 아니다. `kubectl apply`, Helm install/upgrade, Argo Sync, DB 복원·IAM 변경을 이 문서에서 실행하지 않는다.

기존 Argo는 Application 0·대상 권한 없음·deny-all default Project를 유지한다. 모니터링은 기존 GMP 설정을 보존하며 초기 static target 2개만 수집한다. UI에서 가져온 6패널 `adtgr97` JSON은 Grafana API 정의이며 Kustomize 리소스나 자동 file provisioning에 넣지 않았다.

대시보드 구성 이전은 사용자 Windows 직접 확인으로 PASS다. 6패널의 `No data`는 실제 업무 지표가 아직 수집되지 않았음을 뜻한다. 새 수동 세션 logout은 사용자 확인 PASS이며 과거 자동 세션 logout은 UNVERIFIED로 남긴다. DEV 데이터 복원은 `GKE_02F_RESTORE_GATE_HOLD`를 유지한다.
