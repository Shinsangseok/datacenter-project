# GKE Argo CD bootstrap

이 경로는 이미 설치·검증한 GKE-03B의 Argo CD 구성을 Git에 보존한다. 대상은 `datacenter-gke`의 `argocd` Namespace이며, AWS Argo CD의 Application·인증정보·클러스터 등록을 가져오지 않는다. Git 통합 자체는 설치, 재적용, 동기화 또는 권한 변경 승인이 아니다.

## 포함 범위

`gitops/argocd/gcp`의 Kustomize 결과는 **35개** 객체다.

| 구분 | Git 객체 수 |
| --- | ---: |
| Namespace | 1 |
| Argo CRD | 3 |
| deny-all `default` AppProject | 1 |
| runtime core | 23 |
| NetworkPolicy | 7 |
| 합계 | 35 |

실제 GKE-03B 승인 설치 목록은 **38개**였다. 차이인 Secret 3개는 Git에서 제외했다. 자동 생성되는 `kube-root-ca.crt`, 기본 ServiceAccount, ReplicaSet·Pod·EndpointSlice 등은 이 직접 생성 목록에 포함하지 않는다.

runtime core에는 ServiceAccount 4개, 읽기 전용 Role 2개·RoleBinding 2개, ConfigMap 7개, Service 4개, Deployment 3개, StatefulSet 1개가 있다. controller/server/repo-server/Redis는 각각 1 replica다. ClusterRole·ClusterRoleBinding·Application·ApplicationSet·PVC·Ingress·Gateway는 없다. `default` AppProject는 저장소·목적지·허용 cluster resource가 비어 있고, 모든 namespaced resource를 차단한다.

## 고정 버전과 실행 경계

Argo CD `v3.6.1`, 공식 chart `10.17.1`과 실제 설치에 사용한 OCI index digest를 고정했다. 출처와 checksum은 [versions.json](../../gitops/argocd/gcp/versions.json)에 있다. `runtime/core.yaml`은 최종 실행 승인본의 23개 객체를 그대로 옮긴 파일이다. upstream chart render 이후 넓은 RBAC와 불필요한 구성 요소를 제거한 결과이므로, chart 기본값이나 별도 `helm install/upgrade`로 재생성하지 않는다. 이미지 서명 검증을 완료했다는 의미는 아니다.

이 설치는 **읽기 전용 bootstrap** 상태다. controller/server Role은 `argocd` 안의 ConfigMap·Secret·Argo CRD에 `get/list/watch`만 허용한다. Secret 읽기는 인증정보 접근 권한이므로 이 Namespace에 무관한 서비스 Secret을 두지 않는다. repo-server/Redis에는 Role이 없고 API token을 mount하지 않는다. DEV·PROD 대상 Namespace 권한은 없다.

`cluster.inClusterEnabled=false`, 빈 `application.namespaces`, 별도 installation ID·tracking 설정을 보존한다. Application이 없으므로 automated sync·prune·self-heal 실행도 없다. Application·대상 권한·저장소·AWS 등록을 추가하려면 별도 검토와 승인이 필요하다. 이 디렉터리를 관리하는 자기 자신용 Application도 만들지 않는다.

## 외부 Secret 의존성

이름·필수 키·TLS SAN 계약만 [secret-dependencies.json](../../gitops/argocd/gcp/secret-dependencies.json)에 기록했다. 실제 값·비밀번호 hash·CA/개인 키·기본 인증값은 Git에 없다.

| 기존 Secret | 용도 |
| --- | --- |
| `argocd-secret` | 서명 키, 초기 admin bcrypt hash·mtime |
| `gke03b-argocd-redis-auth` | Redis 인증 |
| `gke03b-argocd-redis-tls` | Redis leaf 인증서·개인 키·CA |

기존 Secret을 유지한다. 이 Git 통합을 위해 값을 조회·복사·재생성·회전하지 않는다. 별도 환경의 재구축에는 외부 Secret 사전 준비와 사용자 승인이 필요하다. 누락되거나 유효하지 않으면 HOLD하며 Argo Role에 Secret/ConfigMap 쓰기 권한을 추가하지 않는다. 필수 서명·계정 상태를 미리 준비해야 읽기 전용 설정으로 기동할 수 있다.

## 네트워크와 최종 DNS 보완

default-deny와 기존 7개 정책을 유지한다. DNS 정책에는 실제 GKE-03B DNS 수정으로 검증한 두 peer가 있다. 각각 **동일 peer 안의** Namespace selector `kube-system`과 Pod selector `k8s-app=kube-dns` 또는 `node-local-dns`의 AND 조건이며, UDP/TCP 53만 허용한다. 예전 kube-dns-only 초안이나 임의 link-local IP 허용으로 되돌리지 않는다.

API egress는 관측 당시의 `34.118.224.1/32`, `10.20.0.2/32` TCP 443이며 cluster 주소가 바뀌면 재검증한다. 내부 repo 8081, Redis TLS 6379만 추가 허용한다. 외부 Git·AWS·인터넷·metadata egress는 허용하지 않는다. 이미지 pull의 노드 경로는 Pod NetworkPolicy와 별개다.

UI Service는 ClusterIP HTTP이며 localhost port-forward만 사용한 bootstrap이다. 공개 HTTPS 서비스가 아니다. repo-server 내부 TLS는 strict peer 검증이 꺼져 있고, 실제 Git/RPC 동작·repo self-signed peer trust는 미검증이다. Redis 원격 CA/SAN TLS·NOAUTH 응답은 검증했으나 유효 Redis 인증값 수락을 별도 원격 probe로 입증한 것은 아니다.

## 오프라인 확인과 후속 변경

저장소 루트에서 `kubectl kustomize gitops/argocd/gcp`로 35개 객체를 오프라인 렌더링할 수 있다. 이 root Kustomization은 Git inventory 확인용이며, Secret과 생성 순서를 해결하는 배포 명령이 아니다. 기존 클러스터에는 이미 같은 객체가 있으므로 일괄 create·강제 덮어쓰기·prune을 실행하지 않는다.

새 환경에 승인된 재구축이 필요하면 Namespace → NetworkPolicy 7개 → CRD 3개 및 Established 확인 → deny-all default AppProject → 외부 Secret 3개 → runtime core 순서를 따른다. CRD 생성은 cluster-scoped 권한이 필요하지만 Argo 자체에 cluster RBAC를 주지는 않는다. CRD 삭제는 다른 사용자 정의 리소스도 삭제할 수 있어 자동 rollback·삭제를 하지 않는다.

최종 근거는 `gke-03b-argo-bootstrap-20261111T161819+0900`, 실제 설치·검증 패키지, `gke-03b-dns-fix-20261111T191859+0900`이다. 최종 DNS 정책 spec SHA-256은 `d657ae7a2dbe78026f59046e7209fe6cfb4d2e9eaeedee146da7bc909e57669e`다. 이 통합은 기존 4 Pod·image digest·RBAC 제한·localhost 인증·DNS/API/TLS 검증 결과를 재사용하며, 현재 클러스터 상태를 다시 조회하거나 AWS 서비스·DB·데이터 이전 완료를 판정하지 않는다.

공식 고정 출처: [chart 10.17.1](https://github.com/argoproj/argo-helm/releases/tag/argo-cd-10.17.1), [Argo CD v3.6.1](https://github.com/argoproj/argo-cd/releases/tag/v3.6.1), [서버 bootstrap 동작](https://github.com/argoproj/argo-cd/blob/v3.6.1/server/server.go).
