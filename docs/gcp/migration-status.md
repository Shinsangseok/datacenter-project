# AWS → GCP 이전 현황

전체 목표는 EC2 3대와 공유 RDS의 서비스·데이터·운영 의존성을 GCP로 옮겨 AWS가 서비스 운영에 필요하지 않은 상태를 만드는 것이다. **그 목표는 아직 달성되지 않았다.** GKE-03B Argo bootstrap, GKE-03C 모니터링 기반 및 GKE-03C2 대시보드 구성 이전 PASS는 전체 시스템 이전 PASS가 아니다.

이 문서는 기존 운영 증거를 재사용한다. 전체 34행 인벤토리는 GKE-03A의 `AWS_SERVICE_INVENTORY.json`과 GKE-03C2 render-confirmed의 `AWS_MIGRATION_STATUS.json`을 기준으로 묶었다. GKE-06에서 AWS 실행 위치·데이터를 새로 조회한 것은 아니며, prior/UNVERIFIED 표시는 그대로 유지한다.

## AWS 네 가지 운영 단위

| 운영 단위 | 보존할 서비스·데이터 | GCP 이전 상태 |
| --- | --- | --- |
| EC2 #1 — K3s Server | PROD FastAPI·테스트 변형·simulator, DEV/PROD MLflow, Airflow control/DEV task, Prometheus/Grafana/renderer, Traefik, 관련 PVC·로컬 파일·인증/TLS | 일부 기반만 별도 구축. 업무 서비스·데이터·PROD 전환 미완료 |
| EC2 #2 — Dify | API/web/worker/beat/websocket/agent, plugin daemon·sandbox, nginx·SSRF proxy, PostgreSQL·Redis·Weaviate 및 업로드·plugin·vector·설정·TLS 등 종속 데이터 | 이전 대상 GKE 또는 별도 GCP 서비스 결정 대기. 앱만 옮기거나 PostgreSQL을 Cloud SQL MySQL에 대입하지 않음 |
| EC2 #3 — K3s DEV Worker | DEV FastAPI, AWS Argo CD/GitOps와 관련 runtime·인증/설정 | DEV 미배포. 최신 전체 배치 증거에서 AWS Argo 7 Pod가 이 worker에 있었으므로 DEV 앱만 옮겨도 종료 불가 |
| 공유 AWS RDS | DEV 업무·Tracking/Registry·Auth DB, PROD 및 확인 대상 기타 schema | Cloud SQL DEV 인프라는 생성됐지만 실제 backup/restore는 미실행. 전체 카탈로그·trigger·권한·정합성 Gate 유지 |

## 서비스별 진행 상태

| 기존 34행 묶음 | 현재 확인 범위 | 남은 이전·검증 |
| --- | --- | --- |
| DEV FastAPI — 행 1 | GKE-02A Overlay 18/18 정적 PASS, 승인 GHCR Digest·Pull 결과 재사용. 실제 GKE 앱 미배포 | DEV DB·MLflow 모델/인증/CA, 최소 네트워크·Secret·기동·rollback 검증 |
| PROD FastAPI·auth/registry test·simulator — 행 2–4, 13 | AWS 운영 배치 증거 보존. PROD GCP 실행 없음 | DEV 안정성 확인 후 별도 PROD 데이터·호출자·트래픽·writer 전환 승인. 테스트 객체는 보존/정리 여부 별도 결정 |
| DEV/PROD MLflow — 행 5–6 | Tracking·Registry·Auth·Artifact 보존 계약 및 custom runtime 식별 근거 재사용 | 일관 checkpoint, 전체 객체·trigger/DEFINER·권한·모델 URI/version·파일 SHA 검증과 GCP 런타임 구현. PROD는 별도 격리 대상 |
| Airflow control/DEV tasks — 행 7–8 | 기존 이미지 고정의 0-replica planned 초안 | 실제 Metadata 엔진/DB·암호화 키·Connections·DAG/plugin/log·executor/KPO·현재 task/queue·단일 scheduler 소유권 확인 및 복원 |
| Prometheus — 행 9 | GKE 새 기반 설치 PASS; 초기 static target 2개. AWS 11 job/13 target, retention 및 파일 metadata 조사 재사용 | AWS 전체 scrape 대상·node/exporter·업무 지표, TSDB/WAL 이력과 backup/restore·retention 정책. 새 수집이 AWS 이력 보존을 대신하지 않음 |
| Grafana/renderer — 행 10 | Org1 `adtgr97` 1개 create-only·저장 검증·사용자 6패널 렌더 PASS. 기존 4패널 bootstrap 보존 | 각 6패널 `No data`: 업무 수집 미완료. 전체 Grafana DB/사용자/키/DEK/plugin/renderer·다른 Org와 alert/contact 범위는 별도 보존·검증 |
| Argo CD/GitOps — 행 11 | GKE bootstrap PASS: 공개 Git 객체 35개와 외부 Secret 3개, 4 component, Application 0·deny-all Project | AWS Application·repo/cluster 등록·권한·GitOps 소유권 이전은 미완료. 기존 AWS controller를 무작정 종료하지 않음 |
| Traefik / HTTPS Gateway — 행 12 | DEV/Airflow용 03A Gateway 초안만 planned HOLD | API/class, 실제 domain·DNS·인증서·인증 경계·backend trust·공개 노출 승인과 cutover/rollback |
| Dify 16 container — 행 14–29 | 기존 container ID/process 및 저장소 metadata 근거. 일부 image/env/mount 참조는 prior 증거 연결이고 전체 health·workflow writer는 미검증 | 모든 종속 DB·vector·Redis·파일·plugin·auth/TLS를 묶어 일관 backup/restore, 모델/외부 API 연계·처리 중 작업과 목적지 결정 |
| DEV/PROD/기타 RDS — 행 30–31 | DEV 3 DB 식별과 기존 MySQL/TLS 증거, 별도 Cloud SQL MySQL 8.4.11 인프라 | 현재 승인된 source auth/카탈로그/trigger·일관 backup/별도 restore 증거. PROD 업무 DB 및 기타 미확인 항목은 UNVERIFIED 유지 |
| MLflow test/migration DB·PVC, 중지 host Docker/LLM, Secret/TLS — 행 32–34 | 보존 대상 목록과 prior/UNVERIFIED를 유지 | 소유자별 이전/보관/폐기 결정, 실제 인증·복구·파일 권한·trust 검증. 이름만으로 삭제하거나 완료 처리하지 않음 |

현재 GCP Cloud SQL `datacenter-dev-restore`는 MySQL 8.4.11 Enterprise/private-only로 구축됐다. 승인한 DEV 소스는 `datacenter_app_dev`, `mlflow_tracking_dev`, `mlflow_auth_dev`이며, AWS 소스 MySQL은 8.4.9였다. 연결 성공·테이블 수 1/59/12·동결 전 Artifact SHA는 전체 객체 가시성이나 최신 통합 backup/restore 성공을 대신하지 않는다. source/target UUID는 독립적으로 확인하고 실제 사용자·DB 생성/복원은 별도 승인한다.

## 대시보드 PASS와 세션 증거

GKE-03C2 최종 상태는 `GKE_03C2_DASHBOARD_IMPORT_PASS`다. API 저장·UID·6 PromQL·datasource 참조와 기존 bootstrap 보존 검증에 사용자의 직접 Windows 관찰을 결합했다. `adtgr97`가 열리고 6패널이 모두 표시됐으며 각 `No data`였다. 자동 브라우저 검증 성공이나 실제 metric 내용의 정확성을 주장하지 않는다.

새 수동 브라우저 session logout은 `USER_CONFIRMED_PASS`이며 최종 localhost 13000 listener 0·동일 대상 터널 종료를 확인했다. 과거 자동 session logout 응답은 `UNVERIFIED_UNCHANGED`로 보존한다. 새 사용자의 logout 확인을 과거 session에 소급하지 않는다. 장기 보관을 승인한 외부 0600 인증파일은 Git에 넣거나 일시 RAM profile로 오인해 삭제하지 않는다.

## DEV 데이터 복원과 PROD Gate

`GKE_02F_RESTORE_GATE_HOLD`는 유지한다. DEV 3 DB와 Tracking/Registry/Auth/Artifact를 같은 논리 checkpoint로 보존하려면 현재 writer·full catalog·원본 trigger/DEFINER/권한·실행 경로·독립 target/빈 복원 대상·파일 SHA 및 restore 후 모델 URI/version을 확인해야 한다. 기존 오프라인 복원 CLI·합성 MySQL 실증은 실제 AWS backup/GCP restore 성공이 아니다.

승인 단계는 필요한 GCP 실행 환경·SQL landing/IAM(A), DEV 한정 freeze/drain과 일관 backup(B), 격리 restore와 정합성 검증(C), DEV writer 재개·AWS 원본 확인(D)으로 분리한다. 공유 RDS 전체 lock/stop이나 Dify 전체 중지로 PROD에 영향을 주지 않는다. 복원 후 새 쓰기가 발생하면 AWS 복귀에 delta 보존/reconcile 검토가 필요하며 image rollback이 DB·Artifact rollback을 대신하지 않는다.

실제 UDS 요청별 무쓰기 독립 계측, 기존 AWS 보안 WARN, GKE 독립 비상복구·장기 부하/HA 검증을 임의 PASS 또는 사용자 위험 수용으로 바꾸지 않는다. AWS 종료·삭제는 전체 서비스/데이터/트래픽 이전과 별도 사용자 승인 이후에만 가능하다.

## 후속 로드맵

1. 검토 가능한 Git 기반 소스와 오프라인 CI 통합을 완료하고 현재 bootstrap 권한을 유지한다.
2. DEV source/catalog/writer/trigger Gate를 해소한 뒤 승인된 backup·restore, MLflow·FastAPI 기동·업무 metrics 수집·복구를 검증한다.
3. Airflow와 PROD의 데이터·단일 writer·인증/TLS·Gateway·관찰 기간·cutover/rollback을 별도 승인으로 진행한다.
4. Dify와 모든 종속 데이터, 나머지 운영/보존 대상을 이전하고 전체 34행 인벤토리를 검증한다.
5. 단일 공유 노드의 자원·저장소 I/O·작업 동시성을 계측한다. 부족하면 별도 Node Pool 증설을 제안하고 승인 전 증설하지 않는다.
6. 이후 Kubeflow Pipelines, Istio, 오토스케일링 실습을 진행한다. 이 후속 기능을 현재 설치/검증 완료로 표시하지 않는다.
7. 합의한 안정성·backup/DR·역이전 조건을 충족한 뒤 AWS 중지/정리 여부를 별도 승인받는다.
