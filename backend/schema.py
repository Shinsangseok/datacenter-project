"""Application schema contract; runtime validation performs SELECT only."""
from sqlalchemy import text

# Moved unchanged from the historical FastAPI startup DDL.
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


def validate_measurements(connection):
    connection.execute(text(
        'SELECT id, measured_at, server_id, cpu, memory, temperature, power, '
        'prediction, probability, created_at FROM measurements LIMIT 0'
    ))
