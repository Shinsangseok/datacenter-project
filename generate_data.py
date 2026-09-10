from pathlib import Path

import numpy as np
import pandas as pd

# 재현 가능한 데이터 생성을 위한 시드 설정
rng = np.random.default_rng(42)
rows = []

# 서버 6대 × 1,000회 측정 = 6,000건
for server_num in range(1, 7):
    timestamps = pd.date_range(
        "2026-09-10", periods=1000, freq="10s", tz="UTC"
    )

    for timestamp in timestamps:
        # 정상 80%, 과부하 10%, 냉각 이상 10%
        scenario = rng.choice(
            ["normal", "overload", "cooling_fault"],
            p=[0.8, 0.1, 0.1],
        )

        if scenario == "normal":
            # 높은 CPU 사용률 자체가 반드시 장애는 아님
            cpu = rng.uniform(10, 95)
            memory = rng.normal(55, 15)
            temperature = 30 + 0.35 * cpu + rng.normal(0, 3)

        elif scenario == "overload":
            # CPU와 메모리 사용률이 함께 높은 상황
            cpu = rng.normal(96, 3)
            memory = rng.normal(95, 3)
            temperature = 30 + 0.35 * cpu + rng.normal(0, 3)

        else:
            # 같은 부하에서도 냉각 이상으로 온도가 높아지는 상황
            cpu = rng.uniform(10, 95)
            memory = rng.normal(55, 15)
            temperature = 30 + 0.35 * cpu + rng.normal(25, 4)

        # 사용률을 0~100% 범위로 제한
        cpu = float(np.clip(cpu, 0, 100))
        memory = float(np.clip(memory, 0, 100))

        # CPU와 메모리 사용률에 따른 가상 전력 소비량
        power = 80 + 2.4 * cpu + 0.3 * memory + rng.normal(0, 8)

        rows.append({
            "timestamp": timestamp.isoformat(),
            "server_id": f"server{server_num:02d}",
            "cpu": round(cpu, 2),
            "memory": round(memory, 2),
            "temperature": round(temperature, 2),
            "power": round(float(max(power, 0)), 2),
            "scenario": scenario,
            "label": int(scenario != "normal"),
        })

# 생성한 데이터를 CSV 파일로 저장
df = pd.DataFrame(rows)
Path("data").mkdir(exist_ok=True)
df.to_csv("data/training_data.csv", index=False)

# 생성 결과 확인
print("Saved: data/training_data.csv")
print(f"Rows: {len(df)}")
print("\nScenario counts:")
print(df["scenario"].value_counts())
print("\nLabel: 0 = NORMAL, 1 = ANOMALY")
print(df["label"].value_counts())