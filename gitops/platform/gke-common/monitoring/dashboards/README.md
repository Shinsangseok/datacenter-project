# Dashboard 관리 경계

`adtgr97.api.json`은 실제 AWS Org1 export에서 변환하여 GKE Grafana에 승인 후 1회 생성한 **Grafana API 요청 정의**다. 원 UID와 6개 PromQL은 보존하고 datasource 참조 6개만 `gke-new-prometheus`로 변경했다. 최종 payload SHA는 `408beb5ce3e225aab0477985d236f1cd8e21d06397455dd1221b0c04b0de75e3`다.

이 디렉터리는 Kustomize build·부팅 ConfigMap·Grafana file provisioning에 포함하지 않는다. `dashboard.grafana.app/v2`를 Kubernetes CRD로 apply하지 않는다. 같은 UID가 이미 존재하므로 자동 재생성·upsert를 실행하지 않는다.

부팅용 4패널 `gke-bootstrap-self-metrics`는 별도 ConfigMap으로 계속 관리한다. 사용자 확인으로 6패널의 정상 표시와 `No data`는 확인했으나 업무 metric·TSDB history 이전은 미완료다. 자세한 실제/미완료 상태와 logout·13000 터널 정리 이력은 [모니터링 문서](../../../../../docs/gcp/monitoring.md)를 따른다.
