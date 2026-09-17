import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote_plus

import joblib
import pandas as pd
from fastapi import FastAPI, HTTPException, Query, Response
from pydantic import BaseModel, ConfigDict, Field
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, generate_latest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import SQLAlchemyError

from backend.dify import run_analysis


BASE_DIR = Path(__file__).resolve().parents[1]
MODEL_PATH = BASE_DIR / "models" / "model.pkl"

DB_HOST = os.environ["DB_HOST"]
DB_PORT = os.getenv("DB_PORT", "3306")
DB_NAME = os.environ["DB_NAME"]
DB_USER = os.environ["DB_USER"]
DB_PASSWORD = os.environ["DB_PASSWORD"]

DB_URL = (
    f"mysql+pymysql://{quote_plus(DB_USER)}:{quote_plus(DB_PASSWORD)}"
    f"@{DB_HOST}:{DB_PORT}/{DB_NAME}?charset=utf8mb4"
)

engine = create_engine(
    DB_URL,
    pool_pre_ping=True,
    pool_recycle=1800,
)

model = joblib.load(MODEL_PATH)

prediction_counter = Counter(
    "datacenter_predictions_total",
    "Total prediction requests",
    ["prediction"],
)

anomaly_counter = Counter(
    "datacenter_anomalies_total",
    "Total anomaly predictions",
)

cpu_gauge = Gauge(
    "datacenter_cpu_percent",
    "Latest CPU usage",
    ["server_id"],
)

memory_gauge = Gauge(
    "datacenter_memory_percent",
    "Latest memory usage",
    ["server_id"],
)

temperature_gauge = Gauge(
    "datacenter_temperature_celsius",
    "Latest temperature",
    ["server_id"],
)

power_gauge = Gauge(
    "datacenter_power_watts",
    "Latest power usage",
    ["server_id"],
)


CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS measurements (
    id BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    measured_at DATETIME(6) NOT NULL,
    server_id VARCHAR(32) NOT NULL,
    cpu DECIMAL(5,2) NOT NULL,
    memory DECIMAL(5,2) NOT NULL,
    temperature DECIMAL(6,2) NOT NULL,
    power DECIMAL(8,2) NOT NULL,
    prediction VARCHAR(16) NOT NULL,
    probability DECIMAL(6,5) NOT NULL,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    INDEX idx_server_time (server_id, measured_at),
    INDEX idx_prediction_time (prediction, measured_at)
)
"""


class PredictRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    server_id: str = Field(min_length=1, max_length=32)
    cpu: float = Field(ge=0, le=100)
    memory: float = Field(ge=0, le=100)
    temperature: float = Field(ge=-50, le=150)
    power: float = Field(ge=0, le=10000)
    measured_at: datetime | None = None


app = FastAPI(
    title="Datacenter AI Anomaly Detection API",
    version="1.0.0",
)


@app.on_event("startup")
def initialize_database() -> None:
    with engine.begin() as connection:
        connection.execute(text(CREATE_TABLE_SQL))


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.get("/ready")
def readiness() -> dict:
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
        return {"status": "ready"}
    except SQLAlchemyError as exc:
        raise HTTPException(
            status_code=503,
            detail="database unavailable",
        ) from exc


@app.post("/predict")
def predict(request: PredictRequest) -> dict:
    features = pd.DataFrame(
        [{
            "cpu": request.cpu,
            "memory": request.memory,
            "temperature": request.temperature,
            "power": request.power,
        }]
    )

    predicted_label = int(model.predict(features)[0])
    probability = float(model.predict_proba(features)[0][1])
    prediction = "ANOMALY" if predicted_label == 1 else "NORMAL"

    measured_at = request.measured_at or datetime.now(timezone.utc)

    if measured_at.tzinfo is not None:
        measured_at = measured_at.astimezone(timezone.utc).replace(tzinfo=None)

    insert_sql = text("""
        INSERT INTO measurements (
            measured_at,
            server_id,
            cpu,
            memory,
            temperature,
            power,
            prediction,
            probability
        )
        VALUES (
            :measured_at,
            :server_id,
            :cpu,
            :memory,
            :temperature,
            :power,
            :prediction,
            :probability
        )
    """)

    try:
        with engine.begin() as connection:
            result = connection.execute(
                insert_sql,
                {
                    "measured_at": measured_at,
                    "server_id": request.server_id,
                    "cpu": request.cpu,
                    "memory": request.memory,
                    "temperature": request.temperature,
                    "power": request.power,
                    "prediction": prediction,
                    "probability": probability,
                },
            )
            measurement_id = result.lastrowid
    except SQLAlchemyError as exc:
        raise HTTPException(
            status_code=503,
            detail="could not save prediction",
        ) from exc

    prediction_counter.labels(prediction=prediction).inc()

    if prediction == "ANOMALY":
        anomaly_counter.inc()

    cpu_gauge.labels(server_id=request.server_id).set(request.cpu)
    memory_gauge.labels(server_id=request.server_id).set(request.memory)
    temperature_gauge.labels(server_id=request.server_id).set(request.temperature)
    power_gauge.labels(server_id=request.server_id).set(request.power)

    return {
        "measurement_id": measurement_id,
        "server_id": request.server_id,
        "prediction": prediction,
        "probability": round(probability, 5),
        "measured_at": measured_at.isoformat() + "Z",
    }


@app.get("/history")
def history(
    server_id: str | None = None,
    limit: int = Query(default=20, ge=1, le=100),
) -> list[dict]:
    where_clause = ""
    params = {}

    if server_id:
        where_clause = "WHERE server_id = :server_id"
        params["server_id"] = server_id

    query = text(f"""
        SELECT
            id,
            measured_at,
            server_id,
            cpu,
            memory,
            temperature,
            power,
            prediction,
            probability,
            created_at
        FROM measurements
        {where_clause}
        ORDER BY id DESC
        LIMIT {limit}
    """)

    try:
        with engine.connect() as connection:
            rows = connection.execute(query, params).mappings().all()
    except SQLAlchemyError as exc:
        raise HTTPException(
            status_code=503,
            detail="could not read history",
        ) from exc

    result = []

    for row in rows:
        item = dict(row)
        item["cpu"] = float(item["cpu"])
        item["memory"] = float(item["memory"])
        item["temperature"] = float(item["temperature"])
        item["power"] = float(item["power"])
        item["probability"] = float(item["probability"])
        item["measured_at"] = item["measured_at"].isoformat()
        item["created_at"] = item["created_at"].isoformat()
        result.append(item)

    return result


def build_analysis_context(
    server_id: str | None,
    minutes: int,
    anomaly_limit: int,
) -> dict:
    now_utc = datetime.now(timezone.utc)
    since_utc = now_utc - timedelta(minutes=minutes)

    # MySQL DATETIME에는 현재 timezone 정보 없이 UTC로 저장하고 있으므로
    # 조회 조건도 naive UTC datetime으로 맞춘다.
    since_db = since_utc.replace(tzinfo=None)

    filters = ["measured_at >= :since", "measured_at <= :until"]
    params = {
        "since": since_db,
        "until": now_utc.replace(tzinfo=None),
    }

    if server_id:
        filters.append("server_id = :server_id")
        params["server_id"] = server_id

    where_clause = " AND ".join(filters)

    summary_sql = text(f"""
        SELECT
            COUNT(*) AS sample_count,

            COALESCE(
                SUM(
                    CASE
                        WHEN prediction = 'ANOMALY' THEN 1
                        ELSE 0
                    END
                ),
                0
            ) AS anomaly_count,

            AVG(cpu) AS avg_cpu,
            MAX(cpu) AS max_cpu,

            AVG(memory) AS avg_memory,
            MAX(memory) AS max_memory,

            AVG(temperature) AS avg_temperature,
            MAX(temperature) AS max_temperature,

            AVG(power) AS avg_power,
            MAX(power) AS max_power,

            MAX(measured_at) AS latest_measured_at

        FROM measurements

        WHERE {where_clause}
    """)

    recent_anomaly_sql = text(f"""
        SELECT
            id,
            measured_at,
            server_id,
            cpu,
            memory,
            temperature,
            power,
            prediction,
            probability

        FROM measurements

        WHERE {where_clause}
          AND prediction = 'ANOMALY'

        ORDER BY measured_at DESC, id DESC

        LIMIT :anomaly_limit
    """)

    try:
        with engine.connect() as connection:
            summary = connection.execute(
                summary_sql,
                params,
            ).mappings().one()

            recent_rows = connection.execute(
                recent_anomaly_sql,
                {**params, "anomaly_limit": anomaly_limit},
            ).mappings().all()

    except SQLAlchemyError as exc:
        raise HTTPException(
            status_code=503,
            detail="could not build analysis context",
        ) from exc

    sample_count = int(summary["sample_count"] or 0)
    anomaly_count = int(summary["anomaly_count"] or 0)

    if sample_count > 0:
        anomaly_rate = anomaly_count / sample_count
    else:
        anomaly_rate = 0.0

    def to_float(value):
        if value is None:
            return None

        return round(float(value), 2)

    recent_anomalies = []

    for row in recent_rows:
        recent_anomalies.append(
            {
                "measurement_id": int(row["id"]),
                "measured_at": row["measured_at"].replace(tzinfo=timezone.utc).isoformat(),
                "server_id": row["server_id"],
                "cpu": float(row["cpu"]),
                "memory": float(row["memory"]),
                "temperature": float(row["temperature"]),
                "power": float(row["power"]),
                "prediction": row["prediction"],
                "probability": float(row["probability"]),
            }
        )

    latest_measured_at = summary["latest_measured_at"]

    return {
        "scope": {
            "server_id": server_id or "ALL",
            "window_minutes": minutes,
            "since": since_utc.isoformat(),
            "until": now_utc.isoformat(),
        },

        "summary": {
            "has_data": sample_count > 0,
            "sample_count": sample_count,
            "anomaly_count": anomaly_count,
            "anomaly_rate": anomaly_rate,

            "avg_cpu": to_float(summary["avg_cpu"]),
            "max_cpu": to_float(summary["max_cpu"]),

            "avg_memory": to_float(summary["avg_memory"]),
            "max_memory": to_float(summary["max_memory"]),

            "avg_temperature": to_float(
                summary["avg_temperature"]
            ),
            "max_temperature": to_float(
                summary["max_temperature"]
            ),

            "avg_power": to_float(summary["avg_power"]),
            "max_power": to_float(summary["max_power"]),

            "latest_measured_at": (
                latest_measured_at.replace(tzinfo=timezone.utc).isoformat()
                if latest_measured_at
                else None
            ),
        },

        "recent_anomalies": recent_anomalies,
    }


@app.get("/analysis/context")
def analysis_context(
    server_id: str | None = None,
    minutes: int = Query(
        default=30,
        ge=1,
        le=1440,
    ),
    anomaly_limit: int = Query(
        default=5,
        ge=1,
        le=20,
    ),
) -> dict:
    return build_analysis_context(
        server_id=server_id,
        minutes=minutes,
        anomaly_limit=anomaly_limit,
    )


class AnalysisRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    server_id: str | None = Field(default=None, min_length=1, max_length=32)
    minutes: int = Field(default=30, ge=1, le=1440)
    anomaly_limit: int = Field(default=5, ge=1, le=20)


@app.post("/analysis/run")
def analyze(request: AnalysisRequest) -> dict:
    context = build_analysis_context(
        server_id=request.server_id,
        minutes=request.minutes,
        anomaly_limit=request.anomaly_limit,
    )
    return run_analysis(context)


@app.get("/metrics")
def metrics() -> Response:
    return Response(
        content=generate_latest(),
        media_type=CONTENT_TYPE_LATEST,
    )