# 기존 AWS DEV 객체의 소유권

앱 Kustomization은 `datacenter-app-dev/Deployment/datacenter-api` 하나만 관리한다. 이 문서는 Kubernetes manifest가 아니며 Source에 포함되지 않는다. Service reference YAML은 PR에서 제외하여 실수로 apply할 가능성을 줄였다.

기존 Service는 `datacenter-app-dev/datacenter-api`, ClusterIP `10.43.31.134`, selector `app=datacenter-api`, 포트 8080→8000이다. 실제 최초 Diff/인수 전에 UID·ClusterIP·Endpoint를 재확인한다. Service/Gateway/ServiceLB는 수정·생성·삭제·Argo 인수하지 않는다.

기존 참조 이름:

- ConfigMap: `datacenter-api-config`, `mlflow-rds-ca-75fggf29d8`
- Secret: `datacenter-api-secret`, `datacenter-mlflow-client`, `datacenter-target-policy`

참조만 사용하며 내용·비밀번호·연결 문자열·인증서를 Git으로 복사하지 않는다. PVC/PV, NetworkPolicy, HPA, MLflow도 앱 관리 대상에서 제외한다. DEV FastAPI는 현재 CA ConfigMap volume만 사용하며 새 PVC를 추가하지 않는다. 기존 스토리지와 데이터를 보존한다.
