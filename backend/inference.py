"""Prediction calculation shared by persisted and write-free requests.

Keep the persisted API's evaluation order and error semantics. Validation of
raw outputs belongs only to the verifier, not to this compatibility helper.
"""
from dataclasses import dataclass
from typing import Any

import pandas as pd


@dataclass(frozen=True)
class PredictionResult:
    prediction: str
    raw_probability: float
    raw_predictions: Any
    raw_probabilities: Any


def infer(model, cpu, memory, temperature, power) -> PredictionResult:
    features = pd.DataFrame([{
        "cpu": cpu,
        "memory": memory,
        "temperature": temperature,
        "power": power,
    }])
    raw_predictions = model.predict(features)
    predicted_label = int(raw_predictions[0])
    raw_probabilities = model.predict_proba(features)
    probability = float(raw_probabilities[0][1])
    prediction = "ANOMALY" if predicted_label == 1 else "NORMAL"
    return PredictionResult(prediction, probability, raw_predictions, raw_probabilities)
