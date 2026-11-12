# GKE 모니터링 구성과 이전 상태

이 경로는 실제 설치·검증한 GKE-03C 구성을 Git에서 재현하고 검토하기 위한 소스다. 현재 클러스터에 같은 리소스가 이미 있으므로 Git 편입 자체를 재설치·동기화·삭제 승인으로 해석하지 않는다. AWS 데이터·전체 수집 대상 이전은 완료되지 않았다.

## 구성과 빌드 경계

독립 Kustomize 루트는 `gitops/platform/gke-common/monitoring/`이다.

```sh
kubectl kustomize gitops/platform/gke-common/monitoring
```

위 명령은 로컬 렌더링만 수행한다. 이 루트는 승인한 공개 객체 19개를 생성하며 Secret 객체는 포함하지 않는다. Helm chart를 다시 다운로드하거나 raw Helm render를 직접 적용하는 경로가 아니다. Namespace와 StorageClass도 이 루트가 소유하므로 다른 플랫폼 루트와 합칠 때 같은 이름의 리소스를 중복 등록하지 않는다.

| 종류 | 수 | 실제 이름 또는 경계 |
| --- | ---: | --- |
| Namespace | 1 | `datacenter-observability` |
| StorageClass | 1 | `gke-common-retain-balanced` |
| ServiceAccount | 2 | `gke-prometheus`, `gke-grafana` |
| NetworkPolicy | 6 | 기본 거부, DNS, 양방향 지정 서비스 경로 |
| ConfigMap | 3 | Prometheus 설정, Grafana 설정, 4패널 bootstrap |
| PVC | 2 | Prometheus 20Gi, Grafana 10Gi |
| Service | 2 | ClusterIP 전용 |
| Deployment | 2 | 각 1 replica, Recreate |
| 외부 Secret | 1 | 별도 관리되는 `gke-grafana-admin`; Git 객체 수에서 제외 |

실제 설치의 직접 객체 수는 외부 Secret을 포함해 20개다. Pod·ReplicaSet·PV·VolumeAttachment·자동 default ServiceAccount·공개 `kube-root-ca.crt`는 컨트롤러가 만든 파생 리소스이며 정적 Git manifest로 복제하지 않는다.

## 고정 이미지와 보안·통신

- Prometheus `quay.io/prometheus/prometheus@sha256:9332054523223e2ddc18d8101799d54a4a8d823b030c53216f063a6c117773cd` — 앱 3.16.0, 원 chart 29.43.0.
- Grafana `docker.io/grafana/grafana:13.3.1-distroless@sha256:54e297ac67e2b72572f926892f4e06074dc4f79f2a56ed9a9ca9a88a46f28733` — 앱 13.3.1, 원 chart 14.4.1.

두 실제 runtime imageID는 승인한 index digest와 일치했다. 이미지 서명·SBOM·장기 성능 검증을 뜻하지 않는다. `objects/` 파일은 승인·실제 적용한 본문 그대로 보존했다.

Namespace는 Restricted Pod Security를 적용한다. 컨테이너는 non-root, RuntimeDefault seccomp, 모든 capability 제거, 권한 상승 금지, 읽기 전용 root filesystem을 사용한다. PVC 쓰기 권한은 fsGroup으로 처리하며 root init-chown 컨테이너를 추가하지 않는다. KSA token 자동 mount는 false이며 이 구성에 Role·RoleBinding·ClusterRole·IAM 변경은 없다.

기본 ingress/egress를 거부하고 다음 경로만 허용한다.

- DNS UDP/TCP 53: `kube-system` namespace **AND** `k8s-app=kube-dns` 또는 `k8s-app=node-local-dns` Pod.
- Grafana → Prometheus Pod TCP 9090.
- Prometheus → Grafana metrics Pod TCP 3000. Grafana Service는 80에서 Pod 3000으로 전달한다.

GKE Dataplane V2의 현재 NodeLocal DNS 경로를 반영한다. 169.254 주소나 전체 인터넷·Kubernetes API egress를 추가하지 않는다. Public LB·NodePort·Ingress·Gateway·외부 TLS를 만드는 구성도 아니다. 내부 통신은 HTTP와 NetworkPolicy 격리이며 mTLS 검증 결과가 아니다.

Prometheus의 초기 수집은 정확히 두 static job이다.

| job | 대상 |
| --- | --- |
| `prometheus` | `localhost:9090` |
| `gke-grafana-bootstrap` | `gke-grafana.datacenter-observability.svc.cluster.local:80/metrics` |

Kubernetes service discovery, node/exporter 자동 수집, remote_write, 신규 GMP monitoring CR, Alertmanager, alert rule, contact point를 추가하지 않는다. 기존 GMP는 보존하며 수집 확장은 별도 설계·승인 대상이다. Requests 합계 350m/768Mi, limits 합계 1500m/2816Mi는 초기 시험값이며 전체 AWS 서비스 수용 성능을 검증한 값이 아니다.

## Secret과 영구 데이터

Grafana는 기존 외부 Opaque Secret `gke-grafana-admin`의 다음 키를 참조한다.

| 키 | 참조 |
| --- | --- |
| `admin-user` | `GF_SECURITY_ADMIN_USER` |
| `admin-password` | `GF_SECURITY_ADMIN_PASSWORD` |
| `security-secret-key` | `GF_SECURITY_SECRET_KEY` |

이름·키 참조만 Git에 둔다. 값, 인증파일, token, cookie, 기본 암호 생성 결과는 포함하지 않는다. 기존 Secret이 없는 신규 환경은 승인된 외부 관리 절차가 선행되어야 하며 이 빌드가 값을 생성하거나 기존 값을 복사하지 않는다.

| 용도 | PVC | 요청 |
| --- | --- | ---: |
| Prometheus TSDB | `gke-prometheus-new-empty` | 20Gi |
| Grafana SQLite·상태 | `gke-grafana-new-empty` | 10Gi |

실제 두 PVC는 RWO/Filesystem/Bound였고, 서울의 pd-balanced PD·ext4·Retain PV에 연결되었다. StorageClass는 `pd.csi.storage.gke.io`, Retain, WaitForFirstConsumer, 확장 허용을 사용하며 기본 `standard-rwo`를 수정하지 않는다.

PVC 이름에 `new-empty`가 남아 있어도 설치 후 TSDB·Grafana 정상 데이터가 이미 기록되고 있다. 빈 복원 대상으로 취급하거나 AWS TSDB/WAL/SQLite를 덮어쓰거나 자동 병합하지 않는다. Prometheus retention은 7d/16GB다. Retain과 `helm.sh/resource-policy: keep` annotation은 백업, 관리자 삭제 차단 또는 자동 복구 보장이 아니다. PVC/namespace 삭제 후 Released PV의 재바인딩과 복원 검증은 별도 절차가 필요하다. Grafana의 새 `security-secret-key`는 기존 AWS SQLite·암호화된 datasource/DEK를 자동 복호화하지 않는다.

## 두 Dashboard의 관리 방식

| 항목 | 4패널 bootstrap | AWS에서 가져온 6패널 |
| --- | --- | --- |
| UID | `gke-bootstrap-self-metrics` | `adtgr97` |
| Folder | `GKE Bootstrap` / `gke-bootstrap` | Org1 General/root |
| 관리 경로 | ConfigMap 파일 provisioning | 승인된 Grafana API create-only, 이후 UI 조회 |
| Git 위치 | `objects/11-configmap-gke-grafana-bootstrap-dashboard.yaml` | `dashboards/adtgr97.api.json` |
| Kustomize 포함 | 포함 | **제외** |

6패널 JSON은 **Grafana API 요청 정의**다. `apiVersion=dashboard.grafana.app/v2`는 Kubernetes CRD가 아니며 kubectl apply 대상으로 사용하지 않는다. `metadata.namespace=default`는 Grafana API namespace다. `status={}`는 서버가 제공한 요청 shape에 필요한 빈 envelope이며 AWS 운영 상태를 복사한 값이 아니다.

이 파일을 부팅 ConfigMap·file provider에 자동 추가하지 않는다. 실제 GKE-03C2는 Kubernetes 설정을 변경하지 않고 Grafana에 Dashboard 1개만 생성했다. 기존 4패널 provisioning, datasource, folder, scrape target은 그대로 유지했다. 같은 UID가 이미 있으므로 재생성·upsert·자동 덮어쓰기·GitOps sync 대상도 아니다. 관리 방식을 바꾸려면 기존 API 관리 객체와 file provisioning의 소유권 충돌을 먼저 검토해야 한다.

실제 AWS current Org1 native V2 export를 출처로 삼았다. 원 UID·제목·6패널·6 PromQL·레이아웃·단위·plugin 옵션을 보존하고 datasource 참조만 `prometheus` → `gke-new-prometheus`로 정확히 6곳 변경했다. 현재 Org1의 complete/stable dashboard index는 1개, folder index는 0개였으며 다른 Org·전체 plugin 파일·filesystem 전체 export를 완료한 것은 아니다.

## 실제 검증과 남은 작업

| 범위 | 결과와 한계 |
| --- | --- |
| 03C 설치 | 20 직접 객체, 2 Ready Pod·재시작 0, 승인 digest, Bound Retain PV/PD 2개 확인 |
| 초기 수집 | 두 target이 60초 이상 up, 각 최소 2 scrape 관측. 장기 부하·연속성 시험은 아님 |
| bootstrap | 관리자·datasource health·4패널 정의 검증, 별도 4 PromQL nonempty 결과 수 `[2,1,2,2]` |
| 03C2 API | create-only 1회, 저장 후 UID·6 PromQL·DS 참조 6개·기존 bootstrap 보존 확인 |
| 03C2 시각 확인 | 2026-11-12 사용자 직접 Windows 브라우저 로그인·정상 접근·6패널 표시 확인으로 PASS |
| 6패널 데이터 | 6개 모두 `No data`로 사용자 확인. 정상 빈 표시이며 실제 지표 수집 완료는 아님 |
| 초기 자동 렌더 | 미완료·실패 기록 보존. 사용자 확인으로 자동 검증 성공으로 바꾸지 않음 |
| 세션 정리 | 과거 자동 세션 logout 응답은 **UNVERIFIED 유지**. 새 사용자 수동 세션 logout은 사용자 확인 PASS |
| 터널 정리 | 최종 정리에서 localhost TCP/TCP6 13000 LISTEN 0. 이전 중간 listener 1 기록을 최종 상태로 쓰지 않음 |
| 기존 환경 | 관측 시 Node1, 전체 24 Pod Ready, Argo4 Ready·Application0·default deny Project, GMP 안전 projection 유지 |

`datacenter_cpu_percent`, `datacenter_memory_percent`, `datacenter_temperature_celsius`, `datacenter_power_watts`, `datacenter_predictions_total`은 FastAPI의 업무 지표다. 요청 처리의 DB INSERT 성공 후 counter/gauge가 갱신된다. CPU/온도라는 제목 때문에 node-exporter 지표로 바꿔치기하지 않으며 빈 패널을 채우기 위한 synthetic `/predict` 쓰기를 실행하지 않는다.

실제 업무 workload·DB·모델·scrape 경로 이전 전까지 이 여섯 패널은 `PENDING_WORKLOAD_MIGRATION`이다. node/Kubernetes/exporter 수집은 별도 `PENDING_INFRA_MONITORING_SOURCE`다. AWS 13개 전체 target, Prometheus TSDB/WAL 이력, Grafana SQLite·키·plugin 파일, 알림 설정, 백업·복원·비상복구는 이 Git 편입으로 완료 처리하지 않는다. DEV 데이터 복원 `GKE_02F_RESTORE_GATE_HOLD`와 전체 AWS EC2/RDS 이전 Gate는 유지한다. AWS 종료·노드 증설·추가 배포를 실행하지 않는다.

## 출처와 변경 경계

`gitops/platform/gke-common/monitoring/provenance/verified-state.json`에 출처 package·상대 경로·SHA와 실제/미완료 범위를 요약한다. 상세 인계 package는 다음 순서로 해석한다.

1. `gke-03c-monitoring-20261111T195832+0900`: 승인 19개 공개 객체, 렌더 SHA `c22af364518b275085626215222f5c879dfd9ff0f3e5a97934ffac09d4a7e838`.
2. `gke-03c-monitoring-execution-20261112T094100+0900`: 실제 설치·live·localhost·권한·보존 검증. 로컬 검증기의 단위 계산, empty subPath 직렬화, Service80→Pod3000, classic 변환 API 응답 처리를 고쳤으며 적용 manifest는 바꾸지 않았다.
3. `gke-03c1-monitoring-config-20261112T100956+0900`: 실제 AWS 출처와 6 DS 참조 변환, 최종 API 후보 SHA `408beb5ce3e225aab0477985d236f1cd8e21d06397455dd1221b0c04b0de75e3`.
4. `gke-03c2-dashboard-import-20261112T122548+0900`: 실제 단일 API 생성·저장 검증. 초기 보고서의 자동 렌더 미완료 이력 보존.
5. `gke-03c2-render-confirmed-20261112T153222+0900`: 사용자 직접 6패널·No data 시각 확인.
6. `gke-03c2-session-close-20261112T153819+0900`: 새 수동 logout 확인과 최종 13000 터널 종료. 과거 자동 logout 미검증은 유지.

이번 소스 통합은 로컬 파일 작성·검토다. Secret 원문·개인 키·브라우저 프로필·HAR·인증 로그·운영 DB·Artifact·스크린샷을 Git에 복사하지 않는다. 실제 클라우드 재적용·Grafana import·GitOps 소유권 전환은 수행하지 않는다.
