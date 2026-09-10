import json
import os
import random
import time
from datetime import datetime, timezone
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


# 다른 Namespace에 있는 FastAPI Service 주소
API_URL = os.getenv(
    "DATACENTER_API_URL",
    "http://datacenter-api.datacenter-app.svc.cluster.local:8080/predict",
)

# 가상 서버 수와 데이터 생성 주기
SERVER_COUNT = int(os.getenv("SERVER_COUNT", "6"))
INTERVAL_SECONDS = int(os.getenv("INTERVAL_SECONDS", "10"))


def clamp(value: float, minimum: float, maximum: float) -> float:
    return max(minimum, min(value, maximum))


def generate_measurement(server_id: str) -> tuple[str, dict]:
    # 학습 데이터와 동일하게 정상 80%, 과부하 10%, 냉각 이상 10%
    scenario = random.choices(
        ["normal", "overload", "cooling_fault"],
        weights=[0.8, 0.1, 0.1],
        k=1,
    )[0]

    if scenario == "normal":
        cpu = random.uniform(10, 95)
        memory = random.gauss(55, 15)
        temperature = 30 + 0.35 * cpu + random.gauss(0, 3)

    elif scenario == "overload":
        cpu = random.gauss(96, 3)
        memory = random.gauss(95, 3)
        temperature = 30 + 0.35 * cpu + random.gauss(0, 3)

    else:
        cpu = random.uniform(10, 95)
        memory = random.gauss(55, 15)
        temperature = 30 + 0.35 * cpu + random.gauss(25, 4)

    cpu = clamp(cpu, 0, 100)
    memory = clamp(memory, 0, 100)

    power = 80 + 2.4 * cpu + 0.3 * memory + random.gauss(0, 8)
    power = clamp(power, 0, 10000)
    temperature = clamp(temperature, -50, 150)

    payload = {
        "server_id": server_id,
        "cpu": round(cpu, 2),
        "memory": round(memory, 2),
        "temperature": round(temperature, 2),
        "power": round(power, 2),
        "measured_at": datetime.now(timezone.utc).isoformat(),
    }

    return scenario, payload


def send_measurement(scenario: str, payload: dict) -> None:
    body = json.dumps(payload).encode("utf-8")

    request = Request(
        API_URL,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        with urlopen(request, timeout=5) as response:
            result = json.loads(response.read().decode("utf-8"))

        print(
            f"[OK] {payload['server_id']} "
            f"scenario={scenario} "
            f"prediction={result['prediction']} "
            f"probability={result['probability']}",
            flush=True,
        )

    except HTTPError as exc:
        print(
            f"[HTTP ERROR] {payload['server_id']} "
            f"status={exc.code}",
            flush=True,
        )

    except URLError as exc:
        print(
            f"[CONNECTION ERROR] {payload['server_id']} "
            f"reason={exc.reason}",
            flush=True,
        )

    except Exception as exc:
        print(
            f"[ERROR] {payload['server_id']} "
            f"{type(exc).__name__}: {exc}",
            flush=True,
        )


def main() -> None:
    print(f"Simulator started: API_URL={API_URL}", flush=True)
    print(
        f"Servers={SERVER_COUNT}, interval={INTERVAL_SECONDS}s",
        flush=True,
    )

    while True:
        for server_num in range(1, SERVER_COUNT + 1):
            server_id = f"server{server_num:02d}"
            scenario, payload = generate_measurement(server_id)
            send_measurement(scenario, payload)

        time.sleep(INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
