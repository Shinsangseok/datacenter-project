# AWS DEV FastAPI GitOps 준비

이 경로는 `datacenter-app-dev/Deployment/datacenter-api` 한 개의 향후 수동 인수를 준비한다. PR 생성·CI 성공·병합은 Kubernetes 적용이나 Sync 승인이 아니다. Argo CD v3.5.6은 CD-01A에서 이미 설치됐다. 이 PR에는 설치 manifest, CRD, RBAC, PROD manifest가 없다. 기존 `k8s/`는 현재 AWS Live와 일치하지 않아 GitOps Source로 사용하지 않는다.

## 관리 경로

| 경로 | 용도 |
|---|---|
| `apps/fastapi/base` | Deployment 공통 계약. AWS 참조가 없어 단독 배포하지 않음 |
| `overlays/aws/dev-rollback` | 초기 Diff/인수 및 승인된 rollback: 기존 원본 이미지 + Never |
| `overlays/aws/dev` | 별도 승인 후 전환할 GHCR Candidate + IfNotPresent |
| `argocd/dev` | 차단된 독립 Application + 제한된 AppProject. 앱 Source로 사용하지 않음 |
| `references/aws-dev/OWNERSHIP.md` | 기존 Service/Gateway/설정/스토리지의 소유권 경계 |

앱 overlay는 각각 DEV Deployment 하나만 렌더한다. Namespace, Service, Gateway, Secret, ConfigMap, PVC/PV, HPA, NetworkPolicy, MLflow를 생성·인수·변경하지 않는다. `gitops/` 전체를 재귀 apply하거나 Source로 지정하지 않는다.

## 이미지와 실제 변경 범위

- 기존/rollback: `docker.io/library/datacenter-api@sha256:3fd70c2d1e2ded77e496d3bbbb9f1a72730fb5e0d6c7ba3612c26d086deb079e`, `imagePullPolicy: Never`.
- GHCR 후보: `ghcr.io/shinsangseok/datacenter-api@sha256:930692df197f2579aa73aec466782808de4f94cb70add1f6c95713dbf6cfc113`, `imagePullPolicy: IfNotPresent`.

기존 이미지의 소스 기준은 `39c0e2c`, GHCR 후보는 `abd44eb3fe4ef248f2df2e0cf9bab050055d181a`다. 소스 비교에서 `backend/main.py`, `backend/model_loader.py`, `backend/startup.py`가 다르다. 기동 재시도·모델 로딩 관련 코드가 포함된 새 애플리케이션 버전이며 단순 Registry 이전이 아니다. 실제 의존 패키지·모델·RDS/MLflow 런타임 동등성은 이 PR에서 검증하지 않았다.

DEV/rollback spec 차이는 image와 imagePullPolicy 두 항목이다. 기존 Worker hostname/DEV selector와 정확한 NoSchedule toleration, replicas1, Recreate, env 참조, CA mount, probes, resources, 비루트 보안 설정을 유지한다. startupProbe 추가나 기존 liveness/readiness 완화는 포함하지 않는다. GHCR 앱은 120초 재시도 목표 예산을 갖지만 현재 probe 조합에서의 실제 정상 기동은 별도 승인된 런타임 검증이 필요하다.

## 초기 차단과 권한 경계

Application은 `overlays/aws/dev-rollback`을 대상으로 하고 `targetRevision: NOT_APPROVED_CD01B2`를 유지한다. CD-01B-2에서 병합된 원격 commit을 검토한 뒤 그 40자리 SHA로 등록용 사본을 고정해야 한다. 자기 commit SHA를 자기 파일에 넣거나 가변 main을 실행 승인으로 취급하지 않는다. placeholder 상태에서는 저장소 비교가 완료되지 않는 것이 의도된 차단이다.

`automated.enabled`, `prune`, `selfHeal`, `allowEmpty`는 모두 false다. ApplicationSet, cascade 삭제 finalizer, 자동 namespace 생성, Replace/Force 옵션은 없다. Deployment에는 `Prune=false,Delete=false`, Application에는 `FailOnSharedResource=true`를 유지한다.

AppProject는 지정 GitHub repo와 `datacenter-app-dev` destination만 허용한다. Namespace 리소스는 apps/Deployment만 허용하고 cluster resource allowlist는 비어 있다. `argocd`와 PROD namespace는 배포 destination에 포함하지 않는다. `argocd` metadata namespace는 Application/Project 자체의 위치이며 앱 배포 목적지가 아니다. 상시 deny sync window와 manualSync=false를 유지한다.

CD-01A에서 Controller/API Server의 DEV 조회·DEV/PROD 쓰기 거부 32건을 확인했다. 현재 Kubernetes 권한은 DEV observe뿐이며 DEV sync write 역할은 부여하지 않았다. AppProject는 Kubernetes RBAC를 대신하지 않는다. 향후 Deployment patch 권한은 Pod template 전반을 바꿀 수 있으므로 field-level 보안 경계로 주장하지 않는다. Bootstrap admin의 자체 설정 변경 권한도 남는다.

PROD는 이번 PR에 Application/overlay/RBAC가 없으며 활성화하지 않는다. 향후 PROD는 별도 manifest·권한·명시적 코드/실행 승인과 자동 동기화 OFF 정책으로 검토한다. 로컬에 보관한 과거 설치·PROD 후보를 이 PR의 의존 파일로 사용하지 않는다.

## 검증과 다음 단계

CD-00B의 66 PASS/8개 Kustomize 검증과 CD-01A의 설치·권한 증거를 재사용한다. 이 PR에서는 선택 파일의 독립 렌더 가능성, 변경한 Application, 현재 Live와의 spec 비교 및 비밀정보 제외를 검증한다. GitHub PR CI는 기존 FastAPI offline workflow이며 GitOps admission·실제 Diff·Sync·GHCR 앱 E2E 검증을 대체하지 않는다.

Dex는 CD-01A에서 약105.46Mi/128Mi(82.4%)를 사용했다. 현재 설정은 변경하지 않으며 새로운 Application 부하 전후에 별도 관찰한다. Worker 전체 CPU limit 합계가 물리 CPU를 넘는 위험도 후속 부하 검증 대상이다.

최초 Diff, 이후 인수, GHCR 전환 및 rollback의 승인 경계는 [실행 계획](CD01_RUNBOOK.md)에 따른다.

공식 근거: [Auto Sync](https://argo-cd.readthedocs.io/en/release-3.5/user-guide/auto_sync/), [Sync Windows](https://argo-cd.readthedocs.io/en/release-3.5/user-guide/sync_windows/), [Projects](https://argo-cd.readthedocs.io/en/release-3.5/user-guide/projects/).
