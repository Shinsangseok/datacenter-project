# DEV GitOps 최초 Diff와 후속 실행 계획

현재 범위는 PR 검토까지다. 아래 절차는 후속 승인 계획이며 이 문서가 등록·Sync·권한 확대·배포 승인을 부여하지 않는다. Argo CD 설치는 완료됐으므로 재설치하거나 과거 설치 디렉터리를 재적용하지 않는다.

## CD-01B-2: 병합 후 등록 및 Diff만

1. PR CI 결과, 정확한 변경 목록, main SHA를 검토하고 별도 승인 후 병합한다. 병합 commit SHA를 새로 기록한다. PR merge commit과 branch HEAD를 혼동하지 않는다.
2. 원격 Git에서 그 SHA의 아래 세 경로가 실제로 존재하는지 확인한다. 로컬 Worktree 절대경로를 repoURL로 사용하지 않는다.
3. DEV 기존 Pod/Deployment/Service UID, Ready/재시작, Worker 배치, Argo health·Dex 메모리, 현재 최소 RBAC를 읽기 전용으로 확인한다. 새 장애나 계약 차이가 있으면 HOLD한다.
4. 별도 등록 승인 후 Application의 `NOT_APPROVED_CD01B2`만 승인된 40자리 원격 commit SHA로 바꾼 등록용 사본을 검토한다. source.path는 `gitops/overlays/aws/dev-rollback`이다. Project와 Application만 등록하고 auto-sync=false, deny window, DEV observe-only 권한을 유지한다. ApplicationSet을 만들지 않는다.
5. 저장소 접근·manifest render·실제 Live Diff를 확인한다. 초기 spec 변경은 0이어야 한다. 신규 Argo tracking metadata와 `Prune=false,Delete=false` annotation 차이는 별도로 검토한다. SharedResource 경고나 예상 밖 spec 차이를 무시하지 않는다.
6. 이 단계에서는 Sync, write Role 부여, deny window 해제, Deployment 인수, GHCR 전환을 하지 않는다. 등록/Diff 완료 후 종료한다.

아래는 클러스터를 변경하지 않는 로컬 렌더 명령이다. 별도의 검증 디렉터리에 결과를 보관한다.

```bash
kubectl kustomize gitops/overlays/aws/dev-rollback
kubectl kustomize gitops/overlays/aws/dev
kubectl kustomize gitops/argocd/dev
```

## 별도 승인: 기존 이미지 상태의 최초 인수

정확한 commit/diff와 권한·실행시간·rollback을 승인받은 뒤에만, 기존 `datacenter-api` Deployment 하나의 get/patch/update에 한정된 Kubernetes 역할과 Argo 사용자 sync 권한, 수동 실행 창을 일시적으로 검토·부여한다. 이번 PR에는 해당 write Role/Binding을 포함하지 않았다. create/delete, PROD 권한, cluster-admin은 허용하지 않는다.

원본 이미지/Never인 rollback overlay로 한 번의 수동 인수를 검토한다. prune/force/replace를 사용하지 않는다. Pod template·UID·Node·Ready·재시작·Service Endpoint가 보존되는지 확인하고 실행 후 권한과 window를 다시 닫는다. 예상 밖 Pod 교체나 spec 변경이 필요하면 자동 진행하지 않는다.

## 별도 승인: GHCR 앱 버전 전환

1. GHCR digest의 접근 권한·linux/amd64·Worker image 사용 조건을 별도 확인한다. 이번 작업에서 Pull/Import하지 않는다.
2. `dev-rollback`에서 `dev`로 source를 바꿀 때 diff가 image/Never→IfNotPresent 두 항목인지 다시 확인한다. GHCR 앱의 실제 RDS TLS·MLflow 모델·추론 계약은 CI로 대체하지 않는다.
3. Recreate/replicas1에 따른 DEV 중단과 rollback 감시를 승인받는다. 관찰 기한은 실제 기동 자료로 확정하며 무조건적인 강제 종료 시간을 보장하지 않는다.
4. 승인된 수동 Sync 후 Worker Ready, /health·/ready, Endpoint, RDS 인증, MLflow와 안전한 추론, 운영 서비스·Dex/Worker 자원을 관찰한다. 자동 prune/selfHeal은 계속 끈다.

## 실패 및 rollback

등록/Diff 단계에서 오류가 나면 Sync하지 않고 차단 상태를 유지한다. Application을 지운다고 가정하거나 cascade 삭제를 사용하지 않는다. 실제 인수·이미지 전환 이전에는 앱 workload rollback이 필요하지 않다.

후속 배포 실패 시 첫 실패 로그·lastState·exitCode·reason·finishedAt·이벤트를 비밀값 없이 제한된 시간 안에 확보한다. 로그 수집 때문에 복구를 무기한 지연하지 않는다. 승인된 실행 절차에서 sync를 중단하고 write 권한/window를 통제한 다음, 승인된 원본 `dev-rollback` 경로로 같은 Worker의 기존 Deployment를 복구한다. 원본 image digest는 `3fd70c2d1e2ded77e496d3bbbb9f1a72730fb5e0d6c7ba3612c26d086deb079e`, pullPolicy는 Never다.

원본 이미지 캐시, 대상 UID, Worker 배치가 달라지면 자동 확대 변경하지 않고 HOLD한다. 복구 후 Ready/Endpoint/재시작과 DEV/PROD·MLflow·ServiceLB를 재확인한다. image rollback은 DB·모델 데이터 변경을 복구하지 않는다. PVC/Secret/Service/Gateway/PROD를 rollback 대상으로 삼지 않으며 Namespace/CRD 일괄 삭제는 금지한다.
