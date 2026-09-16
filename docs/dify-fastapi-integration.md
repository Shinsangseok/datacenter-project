# FastAPI → Dify 연동

2026-09-16 K3s 배포 및 실제 Dify/Bedrock 연동 검증을 완료했다.

## 확인한 계약

인증된 `GET http://172.31.51.253/v1/parameters`에서 필수 입력
`incident_context`의 타입이 `paragraph`임을 확인했다.
`POST /analysis/run`은 `GET /analysis/context`와 같은 컨텍스트 생성 함수를 사용하고,
결과를 JSON 문자열로 변환해 `inputs.incident_context`로 전달한다.
Dify 요청은 `/v1/workflows/run`, `response_mode=blocking`, `user=datacenter-api`이다.

DSL의 Code 노드가 읽는 `summary.anomaly_count`, `summary.anomaly_rate`는
FastAPI 컨텍스트와 일치한다. 비율은 0~1이며, 0.15 이상은 HIGH, 0.30 이상은
CRITICAL로 분류한다. 두 심각도는 상세 분석 경로로, 나머지는 간단한 분석 경로로 연결된다.
최종 `data.outputs`는 `{"analysis_result": "분석 문자열"}`이며 FastAPI가 그대로 반환한다.
심각도는 별도 출력 필드로 반환하지 않고, LLM 구조화 출력은 사용하지 않는다.

## FastAPI 요청과 응답

호출 예시는 [README](../README.md#fastapi에서-분석-요청)를 참고한다.

빈 body 객체 `{}`는 전체 서버, 30분, 최근 이상 5건을 사용한다.
`server_id`는 생략 또는 null 가능하며, 지정하면 1~32자이다.
`minutes`는 1~1440, `anomaly_limit`는 1~20이다.

- `sample_count == 0`: HTTP 200, `status=skipped`, `reason=no_data`.
  Dify 설정 확인 및 HTTP 호출 없이 반환한다.
- 성공: HTTP 200, `status=succeeded`, `workflow_run_id`, `outputs`, `context`.
- Dify 설정 누락/오류 및 DB 조회 실패: HTTP 503.
- Dify 인증 실패: HTTP 502, `Dify authentication failed`.
- Dify 통신/HTTP 오류, Workflow 실패, 잘못된 응답: HTTP 502.
- 타임아웃: HTTP 504. 서버에서 Workflow가 계속 실행될 수 있으므로 자동 재시도하지 않는다.

`/predict`에서는 Dify를 자동 호출하지 않는다. 분석은 별도 POST로 요청한다.

## 환경변수

- ConfigMap `datacenter-api-config`: `DIFY_BASE_URL=http://172.31.51.253/v1`,
  `DIFY_TIMEOUT_SECONDS=120` (HTTP 작업 timeout, 총 실행 시간 상한은 아님).
- 기존 Secret `datacenter-app/dify-api-secret`: `DIFY_API_KEY`.
  Deployment에서 `secretKeyRef`로 주입한다.
- HTTP 연결 timeout은 5초이며 자동 redirect와 환경변수 proxy를 사용하지 않는다.
- 키와 upstream 오류 본문은 로그 및 오류 응답에 포함하지 않는다.

## 로컬 검증

```bash
.venv/bin/python -m unittest discover -s tests -v
```

HTTP MockTransport 및 DB 모의를 사용한다. 실제 RDS 접근, Workflow 실행,
배포 없이 입력 직렬화·빈 데이터·인증 실패·타임아웃·실패 응답을 검증한다.

## 배포 및 실제 검증

기존 방식대로 Docker 빌드 후 단일 K3s 노드의 containerd에 import했다.
Deployment는 `imagePullPolicy: Never`를 사용한다.

- 새 이미지: `datacenter-api:1.3-dify-20260916`
- 이전 이미지: `datacenter-api:1.2` (노드에 보존)
- Pod 2개 Ready, 각 Pod의 `/ready` 및 `/analysis/context`: HTTP 200
- 데이터가 있는 `server06`으로 `/analysis/run` 1회 실행: HTTP 200, `status=succeeded`
- `workflow_run_id` 존재 및 `outputs.analysis_result`가 비어 있지 않은 951자 문자열임을 확인
- 타임아웃 및 재호출 없음; 오프라인 회귀 테스트 13건, Code 노드 경계값 검사 6건 통과

현재 배포 직전 Pod 템플릿은 revision 2로 보존되어 있다. 해당 배포 롤백:

```bash
kubectl -n datacenter-app rollout undo deployment/datacenter-api --to-revision=2
kubectl -n datacenter-app rollout status deployment/datacenter-api --timeout=180s
```

다른 환경에서는 먼저 rollout history에서 이전 이미지의 revision을 확인한다.
Secret 값과 원본 DSL, 로컬 배포 백업은 저장소에 포함하지 않는다.
