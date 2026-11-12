# GKE-06 소스 통합 기록

## 작업 기준과 원본 보존

통합 worktree는 `/home/ubuntu/projects/datacenter-project-gke-migration-foundation`, branch는 `feat/gke-migration-foundation`이다. 기준 HEAD는 `2801343e67452e5d5170e341cc77e7717908ec4f`다. 원본 `~/projects/datacenter-project`의 main과 AWS 파일은 변경하지 않는다. worktree를 준비하기 전 원격 동명 branch 부재·기존 PR 0을 확인했다. 이는 이후의 commit/push/PR 게시가 완료됐다는 뜻이 아니며, 실제 publication 결과는 최종 작업 보고를 따른다.

기존 `~/gke-handoffs/`의 검증된 영구 보존본에서 공개 Manifest와 검토된 API 모델만 선택했다. 임시 작업 경로·보고서 전체·실행 중 데이터·인증정보를 Git에 통째로 복사하지 않는다. source→destination SHA와 선택·변환 사유는 통합 산출물의 provenance에 기록한다. 과거 상태 보고를 최신 runtime 실측으로 바꾸지 않는다.

| 근거 | 선택 범위 / 차이 |
| --- | --- |
| GKE-02A | DEV Overlay 원문 3파일과 18/18 정적 검증 재사용. 현재 공통 FastAPI base와 byte 동일한 기준. |
| GKE-03A | platform/Airflow/DEV bootstrap 문서 단위 선택, Airflow 0 replica, DEV Service와 별도 held DEV patch, DEV/Airflow Gateway 경로만 보존. 각 선택 객체의 field는 동일. |
| GKE-03B 및 최종 DNS fix | 실제 Argo 실행 구성 35개 공개 객체를 보존. 03A의 옛 Argo·공통 namespace·넓은 대상 권한 초안으로 되돌리지 않음. |
| GKE-03C 실제 실행 | 공개 객체 19개를 그대로 보존. 외부 Secret 1개와 controller 파생 PV/Pod 등을 정적 복제하지 않음. |
| GKE-03C1/03C2 | 현재 AWS Org1의 6패널 `adtgr97`를 기반으로 변환·생성한 API JSON을 참고 정의로 보존. Kustomize/file provisioning에는 제외. |
| GKE-03C2 render-confirmed/session-close | 사용자 직접 6패널·각 No data 확인 PASS, 새 manual logout PASS·최종 터널 정리, 과거 자동 logout UNKNOWN 유지. |

## 독립 build 루트

다음 8개는 **검토를 위한 독립 build root**다. 포함 관계가 있으므로 행별 수를 더해 신규 리소스 수로 보거나 출력들을 합쳐 일괄 apply하지 않는다. Argo 내부 `runtime/`, `crds/`와 planned `bootstrap/`도 하위 조립 경로이며 별도 설치 batch가 아니다.

| root | 렌더 객체 | 의미 / 중복 경계 |
| --- | ---: | --- |
| `gitops/argocd/gcp` | 35 | 실제 설치된 Argo 공개 구성. runtime·CRD·deny-all Project를 조립하며 외부 Secret 3개 제외 |
| `gitops/platform/gke-common/monitoring` | 19 | 실제 설치된 모니터링 공개 구성. Namespace와 유일한 공통 Retain SC 소유 |
| `gitops/overlays/gcp/dev` | 1 | 원본 02A DEV Deployment. replicas=1·미명시 seccomp를 포함한 과거 정적 계약이며 실제 배포 HOLD |
| `gitops/platform/gke-common/planned` | 19 | 선택한 03A bootstrap만. 실제 Argo/관측 Namespace·SC·PROD 대상은 제외 |
| `gitops/platform/gke-common/planned/dev-platform-prep` | 2 | 원본 DEV Deployment+DEV Service에 기존 03A replicas=0/RuntimeDefault patch. 위 DEV·아래 Service와 중복 |
| `gitops/platform/gke-common/planned/airflow-migration-held` | 6 | Deployment 3개 replicas=0, Service 1개, 빈 restore-target PVC 2개 |
| `gitops/platform/gke-common/planned/dev-service-held` | 1 | ClusterIP DEV Service만. held DEV 후보에도 포함 |
| `gitops/platform/gke-common/planned/gateway-held` | 7 | Gateway, DEV/Airflow Route·HealthCheckPolicy·GFE NetworkPolicy. 공개 노출 HOLD |

03A의 기존 resource 선택 이외에 새 MLflow runtime·namespace·KSA·PVC를 설계하지 않았다. MLflow 구현은 [DEV 문서](dev-and-planned-services.md)의 데이터·custom-image·인증·URI Gate에 남긴다. planned PVC는 실제 모니터링이 소유한 `gke-common-retain-balanced`를 참조하며 별도 SC를 중복 생성하지 않는다.

## 고정 버전과 image Digest

모든 포함 Pod image 필드는 `@sha256`를 사용한다. tag는 출처 설명이며 Digest를 대신하지 않는다. 여기의 runtime 식별과 과거 Pull 성공은 image 서명·SBOM·장기 호환성/성능 검증이 아니다.

| 구성 | 버전 / 원 chart | 고정 image Digest |
| --- | --- | --- |
| Argo CD | v3.6.1 / 10.17.1 | `sha256:296e3d74f449b2ce45265613fcb68c18ed7366488360c8ea32f5f2a93d2e5c54` |
| Redis — Argo 내부 | 8.6.4-alpine | `sha256:2cc044fc5a07c9b701f8f1255a309ae9ad7856e694ac03513bf3648c01e40763` |
| Prometheus | 3.16.0 / 29.43.0 | `sha256:9332054523223e2ddc18d8101799d54a4a8d823b030c53216f063a6c117773cd` |
| Grafana | 13.3.1-distroless / 14.4.1 | `sha256:54e297ac67e2b72572f926892f4e06074dc4f79f2a56ed9a9ca9a88a46f28733` |
| DEV FastAPI | 승인 source `001aeaeb93cc80312e0de1890ecb46c7b4baca16` | `sha256:5358f946c6c4bc853af2687577257821699870ef5e3e9a8d148c68cecadd773c` |
| Airflow — planned | 기존 관측 이미지, 실행 HOLD | `sha256:bef4d135ec7b64658cf64089973edd62cab8fd72203f280c7e0952213c17d912` |

Argo index와 amd64 manifest의 구분·chart archive checksum은 [versions.json](../../gitops/argocd/gcp/versions.json)을 따른다. 03A 초기 Argo·모니터링 태그 후보를 실제 승인 Digest보다 우선하지 않는다. GKE-02A와 Airflow는 기존 검증 Digest를 재사용했으며 새 resolve나 image 교체를 수행하지 않았다.

## Secret·권한·데이터 경계

Secret object와 값은 Git에 없다. 이름/key 계약만 남기고 기존 외부 공급을 유지한다. Argo는 Secret 3개(`argocd-secret`, `gke03b-argocd-redis-auth`, `gke03b-argocd-redis-tls`), 모니터링은 `gke-grafana-admin`을 필요로 한다. DEV/Airflow의 Secret·CA 참조는 각각 [DEV 문서](dev-and-planned-services.md)와 selected Manifest에 이름/key로만 기록한다. CA 공개 인증서/개인 키, 비밀번호 hash, local0600 인증파일, token/cookie, SQL dump·Artifact 파일은 복사하지 않는다.

Argo controller/server는 자기 `argocd` Namespace의 CM·Secret·Argo CRD `get/list/watch`만 갖는 읽기 전용 bootstrap이다. Secret 읽기 권한은 인증정보 접근 능력이므로 metadata-only RBAC로 표현하지 않는다. cluster RBAC·DEV/PROD 대상 쓰기 권한·저장소/외부 cluster 등록을 자동 추가하지 않는다. Application/ApplicationSet 0, default Project deny-all, `cluster.inClusterEnabled=false`, 빈 `application.namespaces`를 보존한다. sync/prune/self-heal이나 자기 자신용 Application도 추가하지 않는다.

모니터링은 자기 KSA token automount=false이며 새 RBAC/IAM·GMP 변경이 없다. 기존 `new-empty` PVC에는 설치 이후 정상 서비스 데이터가 이미 존재하므로 AWS backup을 덮어쓸 빈 대상으로 취급하지 않는다. Retain·resource-policy annotation은 backup/삭제방지/복구 검증을 대신하지 않는다.

6패널 `dashboards/adtgr97.api.json`은 Grafana API `dashboard.grafana.app/v2` 요청 모델이다. Kubernetes CRD가 아니므로 Kustomize 리소스가 아니며 이미 UI/API로 생성된 UID를 자동 재import·upsert·file provisioning하지 않는다. 기존 4패널 bootstrap은 ConfigMap 파일 provisioning으로 관리한다. 6개의 JSON 파일이나 대시보드 6개를 이전했다는 의미가 아니다.

## CI 변경 이유와 오프라인 검증

기존 AWS GitOps safety는 AWS 배포/소유 범위와 보안 계약을 검사한다. 새 GCP 세 경로를 같은 AWS 규칙으로 해석하면 독립 GKE CRD·Namespace·StorageClass와 planned 산출물을 AWS rollout으로 오인한다. 반대로 이 경로들을 무검증으로 제외해서도 안 된다. 따라서 `scripts/gitops_safety.py`와 `.github/workflows/gitops-safety.yml`의 통합 변경은 다음 세 prefix를 전용 `scripts/gcp/validate_foundation.py`로 위임하고 기존 AWS 검사를 유지하기 위한 것이다.

- `gitops/argocd/gcp/`
- `gitops/overlays/gcp/`
- `gitops/platform/gke-common/`

이 위임은 기존 AWS image/overlay 규칙을 완화하거나 GCP 경로의 임의 리소스·Secret·권한 확대를 허용하는 예외가 아니다. 전용 검증은 선택 build·scope·고정 image·Secret exclusion·GitOps/RBAC·planned HOLD 등 GCP 계약을 검사한다. 오프라인 schema·정적 결과는 실제 API admission/CEL/defaulting, Secret 존재/값, runtime 네트워크·DB/모델, 데이터 복원·성능·운영 승인을 입증하지 않는다.

```bash
python3 scripts/gcp/validate_foundation.py --repo . --output /tmp/gke-foundation-validation.json
```

필요한 개별 build도 `kubectl kustomize <위 표의 root>`로만 확인한다. 기존에 완료한 runtime/Pull/모의 테스트를 반복해 새로운 실제 이전 성공처럼 표시하지 않는다. 이 통합에서 apply/install/Sync·IAM 변경·DB backup/restore·AWS 종료는 실행하지 않는다.
