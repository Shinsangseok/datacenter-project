import os
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote_plus

import joblib
import pandas as pd
from fastapi import FastAPI, HTTPException, Query, Response
from pydantic import BaseModel, ConfigDict, Field
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, generate_latest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import SQLAlchemyError


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


@app.get("/metrics")
def metrics() -> Response:
    return Response(
        content=generate_latest(),
        media_type=CONTENT_TYPE_LATEST,
    )