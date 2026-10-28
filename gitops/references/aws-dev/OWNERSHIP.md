# 기존 AWS DEV 객체의 소유권

앱 Kustomization은 `datacenter-app-dev/Deployment/datacenter-api` 하나만 관리한다. 이 문서는 Kubernetes manifest가 아니며 Source에 포함되지 않는다. Service reference YAML은 PR에서 제외하여 실수로 apply할 가능성을 줄였다.

기존 Service는 `datacenter-app-dev/datacenter-api`, ClusterIP `10.43.31.134`, selector `app=datacenter-api`, 포트 8080→8000이다. 실제 최초 Diff/인수 전에 UID·ClusterIP·Endpoint를 재확인한다. Service/Gateway/ServiceLB는 수정·생성·삭제·Argo 인수하지 않는다.

기존 참조 이름:

- ConfigMap: `datacenter-api-config`, `mlflow-rds-ca-75fggf29d8`
- Secret: `datacenter-api-secret`, `datacenter-mlflow-client`, `datacenter-target-policy`

참조만 사용하며 내용·비밀번호·연결 문자열·인증서를 Git으로 복사하지 않는다. PVC/PV, NetworkPolicy, HPA, MLflow도 앱 관리 대상에서 제외한다. DEV FastAPI는 현재 CA ConfigMap volume만 사용하며 새 PVC를 추가하지 않는다. 기존 스토리지와 데이터를 보존한다.

## Deployment 이력 annotation 보존

CD-01B-3에서 `--show-managed-fields=true`로 확인한 아래 값은 Deployment 최상위 metadata의 과거 수동 배포 이력이다.

| 키 | 보존할 값 |
|---|---|
| `deployment.datacenter/image-tag` | `datacenter-api:git-a647d80` |
| `deployment.datacenter/source-revision` | `a647d80dd4f230b26a2fc249994e5d4c4254fd44` |

두 필드의 managedFields 소유자는 `kubectl-client-side-apply`이며 기록 시각은 `2026-09-28T00:58:38Z`다. 기존 `dev-fastapi-e2e-20260928/deploy_api.py`가 `common_api.py`의 HEAD/TAG를 사용해 이 값을 기록했다. 이후 수동 rollout은 top-level annotation을 갱신하지 않았다. 조회 당시 다른 controller의 자동 갱신 또는 해당 작성 스크립트의 실행은 발견되지 않았다. 향후 외부 작성자가 생기면 다시 소유권을 검토해야 한다.

이 값은 현재 원본 이미지의 소스 `39c0e2c`를 증명하지 않는다. 과거 이력을 지우지 않기 위해 `dev-rollback/metadata.patch.yaml`에서만 정확한 기존 값을 유지한다. Pod template annotation은 추가하거나 변경하지 않는다. 기존 `Prune=false,Delete=false`도 보존한다.

GHCR 전환 시에는 실제 이미지 provenance를 별도로 검증하고 새 `image-tag`/`source-revision`, 검사 정책, 예상 metadata 변경을 같은 승인에 포함한다. 현재 GHCR 후보 overlay에는 이 과거 값을 복제하지 않았으므로 후보가 렌더된다는 사실만으로 Sync를 승인할 수 없다. 기존 이력은 전환 직전의 로컬 증거로 보존한다.
