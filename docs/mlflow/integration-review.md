# MLflow 소스 통합 검토 — 운영 미적용

기준은 main `2e4197df0ab6339f99340db3b4b97c4fc1ea8426`과 원격
`feat/mlflow-tracking`이다. 기존 Draft PR #7의 충돌을 최신 main 위에서
선별 통합한 검토 후보이며, PR #7 자체를 변경하거나 main에 병합하지 않는다.
원본 브랜치의 추가 로컬 작업과 Airflow DAG는 이번 통합 범위에 넣지 않는다.

## 소스 선택과 보존 경계

| 영역 | 통합 원칙 |
|---|---|
| FastAPI | 최신 main의 `/predict`, 분석 API, startup/retry, readiness, 인증·DB 환경 검사 계약을 보존 |
| MLflow Tracking / Registry | 학습 기록·Projects와 고정 Version 1 로더를 검토. 모델 실패 시 local/다른 환경 fallback 금지 |
| Auth | 공통 target/runtime/schema guard, read-only client, 환경별 policy와 관련 offline 테스트를 포함. resource validator가 없는 HEAD 등도 fail-closed 거부 |
| DB Migration | 검토된 migration hash·checkpoint·fencing 코드만 포함. 실제 DB 연결·Migration·bootstrap 미실행 |
| Kubernetes | 기존 legacy 파일을 보존하고 DEV/PROD 후보 정의를 별도 디렉터리에 추가. 운영 적용 미실행 |
| CI / GHCR / GitOps | 최신 main의 workflow·수동 게시·승인·GitOps 안전성 경계를 보존 |
| 문서·테스트 | 과거 운영 기록, 이번 소스 검사, 앞으로 필요한 live E2E를 구분 |

기존 로컬 모델의 기본 동작과 Registry의 명시적 선택을 구분한다.
[학습 실행 계약](training.md), [Projects](projects.md),
[추론 로더](inference-loader.md)를 함께 확인한다. Registry version 1의 과거
READY 상태와 6,000개 예측 비교는 원본 브랜치 기록이며 이번 통합에서 실제
Registry에 연결하거나 재학습해 얻은 결과가 아니다.

## 후보 구성과 운영 환경의 차이

- [DEV/PROD 공통 구성](dev-prod-configuration.md)의 namespace·DB target·Secret
  참조는 서로 분리된 소스 계약이다. 실제 hostname·credential·UUID는 포함하지 않는다.
- [DEV 재검증](dev-auth-revalidation.md)과
  [PROD 승격 계획](prod-promotion-runbook.md)은 미실행 gate다. 후보 overlay의
  전체 apply, Secret 생성, Auth 활성화 또는 FastAPI rollout을 승인하지 않는다.
- Migration/bootstrap Job은 suspended 상태다. 선언을 포함했다는 이유로
  Job을 실행하거나 DB·PVC·Secret 데이터를 변경하지 않는다.
- Manifest의 image digest는 원본 브랜치에서 가져온 이전 후보 식별자다.
  이번 통합 commit의 이미지 빌드·게시·실행 검증을 뜻하지 않는다.
- Auth migration hash와 schema contract는 검토된 custom image를 대상으로 한다.
  같은 버전 이름의 upstream package가 이를 만족한다고 가정하지 않는다.
  Hash/target/schema 불일치는 실패로 유지하며 검사를 우회하지 않는다.
- Legacy [Tracking base](tracking-server.md)는 무인증 호환 기록이다.
  현재 운영 배포 경로로 재적용하지 않는다. ClusterIP는 인증 경계가 아니다.

## 검증 결과의 해석

Python 구문·import, Mock·MLflow 회귀 테스트, GitOps Safety, Kustomize render,
문서 링크와 민감정보 정적 검사를 소스 검증으로 구분한다. 최종 실행 결과와
정확한 테스트 수는 통합 PR의 증거에서 확인한다. 과거 문서의 PASS를 이번
commit의 결과로 재사용하지 않는다.

이 검토에서는 Kubernetes·RDS·MLflow·Argo CD에 연결하여 상태를 바꾸지 않는다.
라이브 HTTP 추론·DB INSERT·Training write·Auth bootstrap·PVC 이전·배포·Rollback은
실행하지 않는다. GHCR 후보 배포, DEV MLflow Worker 이전과 GKE 이전도 여전히
별도 검증 대상이다.

기존 공개 저장소 보안 감사의 **WARN**은 유지한다. Dify HTTP 인증 전송,
legacy 외부 API 인증 경계, image·의존성 고정 문제를 해결 완료로 주장하지 않는다.
이 문서는 새로운 PR의 검토 자료이며 운영 준비 완료나 병합·브랜치 삭제 승인이 아니다.
