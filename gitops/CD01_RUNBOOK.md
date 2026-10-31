# DEV GitOps 최초 Diff와 후속 실행 계획

현재 범위는 CD-01B-3 안전장치 코드 검증·병합과 기존 Application의 고정 revision 갱신·Diff까지다. 아래 절차는 후속 승인 계획이며 이 문서가 등록·Sync·권한 확대·배포 승인을 부여하지 않는다. Argo CD 설치는 완료됐으므로 재설치하거나 과거 설치 디렉터리를 재적용하지 않는다.

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
2. `dev-rollback`에서 `dev`로 source를 바꿀 때 spec diff가 image/Never→IfNotPresent 두 항목인지 확인하고, top-level 이력 annotation의 새 값과 기존 이력 보존도 같은 승인에서 검토한다. GHCR 앱의 실제 RDS TLS·MLflow 모델·추론 계약은 CI로 대체하지 않는다.
3. Recreate/replicas1에 따른 DEV 중단과 rollback 감시를 승인받는다. 관찰 기한은 실제 기동 자료로 확정하며 무조건적인 강제 종료 시간을 보장하지 않는다.
4. 승인된 수동 Sync 후 Worker Ready, /health·/ready, Endpoint, RDS 인증, MLflow와 안전한 추론, 운영 서비스·Dex/Worker 자원을 관찰한다. 자동 prune/selfHeal은 계속 끈다.

## 실패 및 rollback

등록/Diff 단계에서 오류가 나면 Sync하지 않고 차단 상태를 유지한다. Application을 지운다고 가정하거나 cascade 삭제를 사용하지 않는다. 실제 인수·이미지 전환 이전에는 앱 workload rollback이 필요하지 않다.

후속 배포 실패 시 첫 실패 로그·lastState·exitCode·reason·finishedAt·이벤트를 비밀값 없이 제한된 시간 안에 확보한다. 로그 수집 때문에 복구를 무기한 지연하지 않는다. 승인된 실행 절차에서 sync를 중단하고 write 권한/window를 통제한 다음, 승인된 원본 `dev-rollback` 경로로 같은 Worker의 기존 Deployment를 복구한다. 원본 image digest는 `3fd70c2d1e2ded77e496d3bbbb9f1a72730fb5e0d6c7ba3612c26d086deb079e`, pullPolicy는 Never다.

원본 이미지 캐시, 대상 UID, Worker 배치가 달라지면 자동 확대 변경하지 않고 HOLD한다. 복구 후 Ready/Endpoint/재시작과 DEV/PROD·MLflow·ServiceLB를 재확인한다. image rollback은 DB·모델 데이터 변경을 복구하지 않는다. PVC/Secret/Service/Gateway/PROD를 rollback 대상으로 삼지 않으며 Namespace/CRD 일괄 삭제는 금지한다.

## CD-01C2: GHCR 후보 검증과 원본 이미지 복귀

1. 후보 metadata 변경 PR의 정확한 head SHA에 대해 Safety CI를 확인한다. 배포 승인을 PR 병합 승인으로 해석하지 않는다. 별도 병합 승인 전에는 Application source/revision, 계정, RBAC, Sync Window를 바꾸지 않는다. 승인 후 실제 새 squash SHA를 기록하고 두 overlay를 그 SHA로 고정한다.
2. `dev`의 이력 annotation은 [소유권 문서](references/aws-dev/OWNERSHIP.md)의 GHCR 태그와 앱 소스 `abd44eb3fe4ef248f2df2e0cf9bab050055d181a`여야 한다. 새 GitOps squash SHA와 앱 소스 SHA를 혼동하지 않는다. `dev-rollback`은 기존 과거 이력 두 값을 유지한다.
3. 실제 Argo Diff에서 후보 변경을 image, imagePullPolicy 및 검토된 Deployment 최상위 이력 annotation 두 값으로 제한한다. Pod template metadata, Worker 배치, Probe/Resource, Secret/ConfigMap 참조 및 다른 spec 변경은 거부한다. 기존 `snapshot/check --live-snapshot`은 원본 이미지의 인수 검증 계약이므로 GHCR 실행 상태의 일반 검증기로 사용하거나 이를 위해 검사를 완화하지 않는다.
4. Worker에서 후보 digest의 실제 Pull 가능성·디스크와 원본 캐시를 확인한다. 기존 최소 권한 및 회수 절차를 재사용하고, 실제 배포 직전 운영 Gate·독립 복구 접근·비노출 운영자 입력이 준비된 경우에만 후보 수동 Sync를 1회 수행한다. Auto Sync/Prune/SelfHeal, Force/Replace는 사용하지 않는다.
5. Recreate/replicas=1에 따른 실제 중단 시간을 기록한다. 사전에 확정한 기동/복구 기한 안에 Worker Ready, HTTP health/ready, Endpoint, RDS TLS/auth 및 DEV MLflow 모델 로딩을 확인한다. 추론은 후보 앱의 실제 HTTP 계약에서 쓰기 없는 경로가 확인될 때만 수행한다. INSERT를 수행하는 `/predict`를 쓰기 금지 검증에 사용하거나 테스트 성공으로 대체하지 않는다. 쓰기 없는 검증 경로가 확인되지 않으면 HOLD하고 별도 방안을 검토한다. 앱 코드, DB/모델 데이터는 변경하지 않는다.
6. 후보가 정상이어도 승인된 `dev-rollback` Source로 수동 Sync 1회 후 원본 digest/Never, 과거 annotation, Worker Ready, HTTP health/ready 및 Endpoint를 확인한다. Argo 실패 시 검증한 독립 Kubernetes 복구 경로를 사용하되 대상 UID/spec/이미지 기준과 최소 권한을 넘지 않는다. 실제 복구가 확인돼야 rollback PASS다.
7. 성공/실패 모두 진행 중 Operation 상태와 정리 순서를 확인하고 임시 권한·토큰을 회수한다. `dev-operator` 비활성화, 임시 sync mapping 제거, Deny Window/manualSync=false 및 Controller 쓰기 거부를 확인한다. 정상 종료는 원본 이미지와 `dev-rollback`, Synced/Healthy이며 GHCR을 다시 적용하지 않는다.



## CD-01B-3: S1–S5 실행 Gate

### S1/S3 — 코드 검사와 로컬 Live 비교

독립 승인된 checkout에서 실행한다. Python 3.12, PyYAML과 Kustomize 5.8.1(또는 해당 버전이 포함된 kubectl)이 필요하다. 검사 스크립트는 Kubernetes 쓰기 명령을 실행하지 않는다.

```bash
python3 -B scripts/gitops_safety.py check --repo .
python3 -B -m unittest discover -s tests -p test_gitops_safety.py -v
```

PR workflow는 표준 ubuntu-latest에서 contents:read만 사용한다. SHA로 고정된 Actions와 checksum으로 고정된 Kustomize를 사용하며 AWS 자격증명, kubeconfig, Secret, packages:write, 배포 단계가 없다. 여기의 정적 PASS는 실제 Live/Apply 검증이 아니다.

운영 전환 직전에는 관리자가 새 전용 Evidence 디렉터리를 Git 밖에 마련한 뒤 다음 READ-ONLY 명령을 실행한다. 아래 경로는 실제 새 경로로 바꾸고 기존 파일을 덮어쓰지 않는다. `--help`의 종료 코드 0은 검사 PASS가 아니다.

```bash
python3 -B scripts/gitops_safety.py snapshot --output /absolute/outside-git/evidence/dev-deployment.json
python3 -B scripts/gitops_safety.py check --repo . --live-snapshot /absolute/outside-git/evidence/dev-deployment.json
```

성공은 종료 코드 0과 JSON `status=PASS`, `live_comparison.status=PASS`, spec/template 차이 0을 모두 확인한다. Snapshot은 UID/generation, 검토된 spec, 보호 annotation과 managedFields 요약만 보존한다. Secret 값과 last-applied 원문은 저장하지 않는다. 알 수 없는 spec 또는 inline 값은 저장 전에 거부하며 알려지지 않은 annotation은 값 대신 hash를 남기고 비교를 차단한다. 원본 live JSON을 우회 저장하거나 Git에 넣지 않는다.

검사기의 정확한 계약은 의도된 초기 인수 범위다. 새로운 정상 변경도 별도 코드 검토·정책 갱신 없이는 FAIL한다. 검사 코드·테스트를 함께 바꾸는 PR이나 관리자 우회까지 막는 보안 경계는 아니다. GitHub branch protection/rulesets의 실제 적용 여부, 필수 status check와 독립 리뷰가 별도로 필요하다. 권한 부족으로 설정을 읽을 수 없으면 UNVERIFIED로 남기고 저장소 관리자가 확인한다. 이번 단계에서 보호 설정을 변경하지 않는다.

### S1/S2 — Argo 실제 Diff 및 소유권

로컬 비교 후 승인된 squash SHA에 대해 Argo의 원격 렌더와 normalized Live/predicted Diff를 확인한다. Application의 source는 dev-rollback, resource는 DEV Deployment 하나여야 한다. spec/template 변경 0, 원본 digest/Never 유지, 이력 annotation 삭제 0이어야 한다. Argo의 tracking-id와 Prune=false,Delete=false 추가는 예상 metadata 변경으로 별도 기록한다. 다른 차이를 ignoreDifferences로 숨기지 않는다.

managedFields, 과거 client-side apply 이력 및 현재 Argo apply 방식을 함께 검토한다. 예상 Diff를 확인해도 실제 Sync를 실행하지 않았다면 `actual_argo_apply=UNVERIFIED`다. UID/generation/Pod가 바뀌었거나 알 수 없는 field owner가 나타나면 다시 검토한다. 두 annotation의 소유권과 과거 이력 의미는 [소유권 문서](references/aws-dev/OWNERSHIP.md)를 따른다.

### S4 — 최초 Sync 별도 승인 체크리스트

다음 열 가지가 기록되기 전에는 Sync하지 않는다.

1. PR의 정확한 head SHA에 대해 GitOps safety CI와 필요한 다른 check 성공.
2. 검토·병합된 원격 40자리 commit과 Application revision의 일치.
3. 새 Live snapshot 비교 및 실제 Argo Diff 검토. 실제 apply의 잔여 불확실성을 승인자가 인지.
4. 예상 변경 목록 명시: 초기 인수의 spec/template 변경 0과 허용된 Argo metadata만.
5. DEV/PROD/MLflow/Airflow/Argo 정상 상태 및 신규 비정상 재시작 없음.
6. Worker의 원본 name@digest 로컬 캐시와 현재 실행 컨테이너 확인.
7. 대상 UID, 원본 spec/overlay, 제한 시간의 실패 증거 수집 및 DEV 한정 복구 절차 준비.
8. 허용 작업·시간 범위·권한 범위와 별도 사용자 실행 승인 기록.
9. PROD, RDS, Secret, PVC, Service/Gateway, MLflow 무변경 확인.
10. 실제 Sync 실행자와 종료 후 권한/window 복구 담당자 확인.

Auto Sync/Prune/SelfHeal/allowEmpty=false, 상시 deny window/manualSync=false, Controller/API Server의 DEV observe-only 및 PROD 쓰기 거부를 각각 확인한다. AppProject와 Kubernetes RBAC는 서로 대체하지 않는다. READ-ONLY `kubectl auth can-i`로 해당 ServiceAccount의 허용/거부를 확인하되 이 문서가 쓰기 Role 부여를 승인하지 않는다. 관리자에게는 설정을 바꿀 권한이 있을 수 있어 물리적으로 우회 불가능하다고 주장하지 않는다.

### S5 — 원본 복구 준비와 후속 승인된 복구

복구 overlay는 원본 digest/Never, Worker hostname+DEV selector, 정확한 DEV taint toleration, replicas=1/Recreate, 기존 probe/resource/설정 참조를 보존한다. Node status의 이미지 캐시 보고와 실행 중 DEV Pod의 imageID를 비교한다. 과거 CRI 검증만으로 미래 캐시 존재를 보장하지 않으며 전환 직전 재확인한다. Manifest digest와 CRI config ID를 혼동하지 않는다.

```bash
kubectl get node datacenter-k3s-worker-dev-01 -o jsonpath='{.status.images[*].names}'
kubectl -n datacenter-app-dev get deployment datacenter-api -o jsonpath='{.metadata.uid}{" "}{.metadata.generation}{"\n"}'
kubectl -n datacenter-app-dev get pods -l app=datacenter-api -o wide
kubectl -n datacenter-app-dev get endpointslices -l kubernetes.io/service-name=datacenter-api -o json
```

후속 이미지 전환의 별도 승인에는 Recreate/1 replica의 DEV 중단 가능성, 상태 확인 기한과 복구 완료 기한, 원본 이미지가 쓰는 DB/모델 계약의 유지 조건을 포함한다. 현재 캐시가 있어도 GC·Node 장애로 삭제될 수 있다. 이미지 rollback 준비 PASS는 실제 rollback 성공이 아니다.

실패 시 최초 이전 컨테이너 로그·lastState·exitCode/reason/finishedAt와 해당 UID 이벤트를 승인된 짧은 수집 시간 안에 보존하고 즉시 복구 절차로 넘어간다. 비밀번호·URL·환경변수 원문을 보고서에 넣지 않는다. 기존 Deployment UID가 일치하고 다른 변경자가 없는지 확인한 뒤, 별도 승인된 실행자가 고정 rollback commit/source를 선택하고 DEV Deployment 하나에만 수동 복구를 수행한다. prune/force/replace 또는 일괄 apply를 사용하지 않는다. UID나 캐시가 다르면 강제 대체·외부 Pull 대신 HOLD하고 복구 담당자에게 보고한다.

복구 후 원본 image/Never, Worker Pod Ready, /health와 /ready HTTP 200, Service Endpoint의 해당 Pod UID/IP 및 DEV/PROD·MLflow·ServiceLB 상태를 확인한다. 쓰기 권한과 window를 다시 닫는다. PVC/RDS/PROD/MLflow/Gateway 복원·삭제 및 DB rollback은 범위 밖이다. 이 단계에서는 실제 rollback도 수행하지 않는다.
