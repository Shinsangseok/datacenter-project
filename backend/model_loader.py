"""Explicit local or fixed Registry model loading; no fallback between sources."""
import os
from pathlib import Path
from tempfile import TemporaryDirectory

import joblib

FEATURES = ["cpu", "memory", "temperature", "power"]
MODEL_NAME = "datacenter-anomaly-detector"
MODEL_VERSION = "1"
TRACKING_URI = "http://mlflow.mlflow.svc.cluster.local:5000"
TRUSTED_TYPES = ["sklearn.tree._tree.Tree"]


class ModelLoadError(RuntimeError):
    """Model could not be safely prepared; application startup must fail."""


def _validate_contract(model):
    from sklearn.ensemble import RandomForestClassifier

    if not isinstance(model, RandomForestClassifier):
        raise ModelLoadError("Registry model must be RandomForestClassifier")
    if list(getattr(model, "feature_names_in_", [])) != FEATURES:
        raise ModelLoadError("Registry model feature order mismatch")
    if list(getattr(model, "classes_", [])) != [0, 1]:
        raise ModelLoadError("Registry model classes must be [0, 1]")
    if not callable(getattr(model, "predict", None)) or not callable(
        getattr(model, "predict_proba", None)
    ):
        raise ModelLoadError("Registry model requires predict and predict_proba")
    return model


def _load_package(directory):
    import yaml
    import skops.io

    root = Path(directory)
    metadata_path = root / "MLmodel"
    model_path = root / "model.skops"
    if metadata_path.is_symlink() or model_path.is_symlink():
        raise ModelLoadError("Model package must not contain symlinked model files")
    metadata = yaml.safe_load(metadata_path.read_text())
    flavor = metadata["flavors"]["sklearn"]
    if flavor.get("serialization_format") != "skops":
        raise ModelLoadError("Registry serialization must be skops")
    if flavor.get("skops_trusted_types") != TRUSTED_TYPES:
        raise ModelLoadError("Registry trusted types mismatch")
    if flavor.get("code") or flavor.get("pickled_model") != "model.skops":
        raise ModelLoadError("Registry package must use model.skops without custom code")
    if flavor.get("sklearn_version") != "1.9.0":
        raise ModelLoadError("Registry model requires scikit-learn 1.9.0")
    if skops.io.get_untrusted_types(file=model_path) != TRUSTED_TYPES:
        raise ModelLoadError("Actual skops untrusted types mismatch")
    # Do not trust a list supplied by the artifact; use the reviewed constant.
    return _validate_contract(skops.io.load(model_path, trusted=TRUSTED_TYPES))


def _load_registry():
    import mlflow
    from mlflow import MlflowClient
    from mlflow.artifacts import download_artifacts

    name = os.environ.get("MLFLOW_MODEL_NAME", MODEL_NAME)
    version = os.environ.get("MLFLOW_MODEL_VERSION", MODEL_VERSION)
    uri = os.environ.get("MLFLOW_TRACKING_URI", TRACKING_URI)
    if name != MODEL_NAME or version != MODEL_VERSION:
        raise ModelLoadError("Only datacenter-anomaly-detector Version 1 is approved")
    if uri != TRACKING_URI:
        raise ModelLoadError("Registry requires the internal MLflow Service URI")
    # The models:/ resolver creates an internal client using fluent URI state.
    # Set both explicitly so it cannot initialize the default local SQL store.
    mlflow.set_tracking_uri(uri)
    mlflow.set_registry_uri(uri)
    client = MlflowClient(tracking_uri=uri, registry_uri=uri)
    entry = client.get_model_version(name, version)
    if entry.status != "READY" or str(entry.version) != MODEL_VERSION:
        raise ModelLoadError("Registry Version 1 is not READY")
    if not entry.source.startswith("mlflow-artifacts:/"):
        raise ModelLoadError("Registry artifact source must use the MLflow proxy")
    with TemporaryDirectory(prefix="datacenter-registry-") as temp:
        path = download_artifacts(
            artifact_uri=f"models:/{name}/{version}", dst_path=temp,
            tracking_uri=uri, registry_uri=uri,
        )
        return _load_package(path)


def load_model(local_path):
    source = os.environ.get("MODEL_SOURCE", "local")
    if source == "local":
        return joblib.load(local_path)
    if source != "registry":
        raise ModelLoadError("MODEL_SOURCE must be local or registry")
    try:
        return _load_registry()
    except Exception as error:
        # No remote URLs, credentials or exception traceback in startup logs.
        raise ModelLoadError(
            f"Registry model initialization failed ({type(error).__name__}); no fallback"
        ) from None
