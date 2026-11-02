import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, Response
from pydantic import BaseModel, ConfigDict, Field
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, generate_latest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.pool import NullPool
from starlette.responses import JSONResponse

from backend.dify import run_analysis
from backend.model_loader import load_model
from backend.db_config import database_url
from backend.inference import infer
from backend.write_free_verifier import WriteFreeVerifier
from backend.startup import (Failure, Startup, StartupError, validate_database,
                             configure_database_timeouts)


BASE_DIR = Path(__file__).resolve().parents[1]
MODEL_PATH = BASE_DIR / "models" / "model.pkl"

# Only activation settings cross into the verifier; no DB credentials or engine.
write_free_verifier = WriteFreeVerifier({
    key: os.environ[key]
    for key in (
        "WRITE_FREE_INFERENCE_ENABLED", "POD_NAMESPACE", "MLFLOW_ENV",
        "MODEL_SOURCE", "WEB_CONCURRENCY", "UVICORN_WORKERS", "UVICORN_RELOAD",
    )
    if key in os.environ
})

startup = Startup()
_database_url = startup.run('configuration', lambda: database_url(os.environ))
engine = startup.run('configuration', lambda: create_engine(
    _database_url, pool_pre_ping=True, pool_recycle=1800, hide_parameters=True,
))

model = load_model(MODEL_PATH, startup=startup)

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


app.state.startup_complete = False


@app.middleware("http")
async def require_startup(request, call_next):
    if request.url.path != '/health' and not app.state.startup_complete:
        return JSONResponse(status_code=503, content={'detail': 'application not ready'})
    return await call_next(request)


@app.on_event("startup")
def validate_database_schema() -> None:
    app.state.startup_complete = False
    # Only startup reads use short timeouts. The inference/write engine above is unchanged.
    preflight = startup.run('configuration', lambda: configure_database_timeouts(create_engine(
        _database_url, poolclass=NullPool, hide_parameters=True,
    ), startup))
    try:
        validate_database(preflight, startup)
    finally:
        try:
            preflight.dispose()
        except Exception:
            failure = Failure('db_cleanup_failed')
            startup._event('db_contract', failure, 1, 'permanent_or_unknown')
            raise StartupError(failure, 'permanent_or_unknown') from None

    def verify_model_ready():
        if model is None:
            raise RuntimeError('Model initialization incomplete')
    # Recheck the shared budget after connection cleanup, before publishing Ready.
    startup.run('model_load', verify_model_ready)
    app.state.startup_complete = True


@app.on_event("startup")
async def start_write_free_verifier() -> None:
    # Registered after the existing schema/model gate; no second load or DB check.
    try:
        await write_free_verifier.start(
            model_provider=lambda: model,
            ready_provider=lambda: app.state.startup_complete,
        )
    except BaseException:
        app.state.startup_complete = False
        raise


@app.on_event("shutdown")
async def stop_write_free_verifier() -> None:
    if write_free_verifier.enabled:
        app.state.startup_complete = False
    await write_free_verifier.stop()


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.get("/startup")
def startup_health() -> dict:
    # One-time initialization gate. No DB/network call and no runtime age limit.
    if not app.state.startup_complete or model is None:
        raise HTTPException(status_code=503, detail="application not ready")
    return {"status": "started"}


@app.get("/ready")
def readiness() -> dict:
    if not app.state.startup_complete or model is None:
        raise HTTPException(status_code=503, detail="application not ready")
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
    calculated = infer(model, request.cpu, request.memory, request.temperature, request.power)
    prediction = calculated.prediction
    probability = calculated.raw_probability

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