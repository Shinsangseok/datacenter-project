# Auth schema fingerprint v2

DEV Auth의 12 tables / head `f1a2b3c4d5e6`를 읽기 전용으로 비교했다. 원래 DEV 기대 schema와 실제 schema의 차이는 11 tables, 25개 문자열 column에 표시되는 명시적 `COLLATE` 절뿐이다. 타입, nullable/default, index, unique/PK/FK/check constraint와 effective charset/collation은 같다. DB 수정이나 migration 재실행이 필요한 drift는 발견되지 않았다.

## 원인과 비교 기준

기존 fingerprint는 `SHOW CREATE TABLE` 원문을 SHA-256으로 해시했다. 기대값은 완성된 lab DDL에서 AUTO_INCREMENT counter를 제거하고 table collation을 DEV의 `utf8mb4_unicode_ci`로 바꿔 예측했다. 이 방식으로 기존 기대 hash 12개를 모두 재현했다. 그러나 DEV의 SHOW CREATE에는 column별 `COLLATE utf8mb4_unicode_ci`가 추가로 출력돼 원문 hash가 달라졌다.

DEV DB/table default와 25개 문자열 column의 effective 값은 모두 `utf8mb4 / utf8mb4_unicode_ci`다. 기대 DDL의 컬럼은 같은 table default를 상속한다. 따라서 이 불일치는 **A: effective 값이 같은 explicit/inherited 표현 차이**다.

lab 자체는 `utf8mb4_0900_ai_ci`를 사용하므로 lab과 DEV의 collation semantics가 같다는 뜻은 아니다. 검토된 migration은 charset/collation을 명시하지 않고 생성 당시 DB default를 상속한다. DEV provisioning의 승인된 default는 `utf8mb4_unicode_ci`였으므로 현재 DEV 결과가 그 의도와 일치한다. lab collation을 DEV에 강제하거나 실제값으로 expected hash를 덮어쓰지 않는다.

| Table | 표현이 달랐던 column (모두 effective utf8mb4_unicode_ci) |
|---|---|
| alembic_version_auth | version_num |
| experiment_permissions | experiment_id, permission |
| gateway_endpoint_permissions | endpoint_id, permission |
| gateway_model_definition_permissions | model_definition_id, permission |
| gateway_secret_permissions | secret_id, permission |
| registered_model_permissions | name, permission, workspace |
| role_permissions | resource_type, resource_pattern, permission |
| roles | name, workspace, description |
| scorer_permissions | experiment_id, scorer_name, permission |
| users | username, password_hash |
| workspace_permissions | workspace, permission |

`user_role_assignments`에는 문자열 column이 없으며 원문 hash도 일치했다.

## 검증 계약

`schema_fingerprint.py`는 DDL의 type 바로 뒤 charset/collation 선언과 DB/table 상속을 해석해 모든 문자열 column에 effective charset/collation을 명시한다. 실제 `information_schema.COLUMNS`의 값과 반드시 교차 검증한다. `CHARACTER SET`만 있으면 table collation이 아니라 그 charset의 기본 collation을 적용한다. 명시적 collation이 다른 charset에 속하거나 메타데이터가 누락되면 실패한다. 이 규칙은 [MySQL column charset/collation](https://dev.mysql.com/doc/refman/8.4/en/charset-column.html)과 [table 상속 규칙](https://dev.mysql.com/doc/refman/8.4/en/charset-table.html)을 따른다.

hash에는 다음을 포함한다.

- 명시적 effective charset/collation으로 직렬화한 전체 DDL.
- table의 effective collation, engine, row format, 생성 옵션, comment.
- 모든 column의 순서, 이름, type, nullable/default, effective charset/collation, extra, comment, generation expression.

index/FK/PK/unique/check, default literal, comment, table option 및 column의 AUTO_INCREMENT 속성은 구조 hash에 보존한다. 다음 AUTO_INCREMENT 값은 DML로 변하는 할당 상태이므로 `next_auto_increment`로 snapshot에 별도 보존한다. migration/checkpoint 비교는 이 값도 정확히 비교한다. bootstrap 전후에는 `users`의 0→1 row와 counter 1→2만 허용하며 나머지 table의 schema/data/counter는 그대로여야 한다. admin 이름·password hash·is_admin은 별도로 검증한다. charset/collation을 무시하거나 전체 문자열에서 COLLATE를 삭제하지 않는다. 지원 범위는 reviewed Auth의 CHAR/VARCHAR/TEXT 계열 SHOW CREATE 형식이다. 지원하지 않는 character type이나 형식은 추측하지 않고 실패한다.

schema hash는 `mysql-auth-schema-v2:<sha256>` 형식이다. 기존 raw hash와 자동 호환되지 않는다. data fingerprint, row count, revision 비교는 기존 방식을 유지한다. 메타데이터 조회는 호출자가 제공한 동일 physical connection에서 SELECT/SHOW만 수행한다. 잠금, reconnect 거부, revision 검토 hash, checkpoint 승인 절차는 그대로 유지한다.

## 현재 적용 범위와 후속 단계

이번 변경은 로컬 코드/회귀 테스트/문서뿐이다. live ConfigMap, policy, Job, DB/schema는 변경하지 않았고 migration/bootstrap은 실행하지 않았다. 기존 실패 Job을 성공으로 바꾸지 않았다.

회귀 fixture의 기대 DDL은 기존 lab에서 독립적으로 재구성한 뒤 **수정 전 expected raw hash 12개와 일치함을 먼저 검증**했다. 운영 증거의 expected-v2 역시 그 기대 DDL과 메타데이터로 만들며, 관측된 DEV schema hash를 복사하지 않는다. v2와 v1 checkpoint는 혼용하지 않는다.

이미 head에 도달했으므로 이 문제를 해결하기 위한 migration 재실행은 필요하지 않다. bootstrap의 schema 전제는 v2의 읽기 전용 비교로 검증할 수 있다. 실행 전에는 검토된 v2 기대 snapshot/validator를 운영 검증 경로에 반영하고, head/data/account/Secret/안정성 preflight를 다시 확인해야 한다. 이 문서나 fixture는 bootstrap 실행 승인이 아니다.
