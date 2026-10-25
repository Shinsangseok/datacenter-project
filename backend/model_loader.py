"""Explicit local or fixed Registry model loading; no fallback between sources."""
import os
from pathlib import Path
from tempfile import TemporaryDirectory

import joblib

from backend.startup import Failure, Startup, StartupError, classify_registry, registry_transport

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


def _registry_target():
    name = os.environ.get("MLFLOW_MODEL_NAME", MODEL_NAME)
    version = os.environ.get("MLFLOW_MODEL_VERSION", MODEL_VERSION)
    environment = os.environ.get("MLFLOW_ENV", "prod")
    if environment not in {"prod", "dev"}:
        raise ModelLoadError("Unknown MLflow environment")
    expected_uri = (TRACKING_URI if environment == "prod" else
                    "http://mlflow.mlflow-dev.svc.cluster.local:5000")
    uri = os.environ.get("MLFLOW_TRACKING_URI", TRACKING_URI if environment == "prod" else "")
    if name != MODEL_NAME or version != MODEL_VERSION:
        raise ModelLoadError("Only datacenter-anomaly-detector Version 1 is approved")
    if uri != expected_uri:
        raise ModelLoadError("Registry requires the internal MLflow Service URI")
    return name, version, uri


def _registry_steps(startup):
    # Validate the target before importing SDKs, creating clients or making requests.
    name, version, uri = startup.run('configuration', _registry_target)
    import mlflow
    from mlflow import MlflowClient
    from mlflow.artifacts import download_artifacts

    mlflow.set_tracking_uri(uri)
    mlflow.set_registry_uri(uri)
    client = startup.run('configuration', lambda: MlflowClient(tracking_uri=uri, registry_uri=uri))

    def lookup():
        with registry_transport(startup):
            return client.get_model_version(name, version)
    entry = startup.run('registry_lookup', lookup, classify_registry)

    def validate_entry():
        if entry.status != "READY" or str(entry.version) != MODEL_VERSION:
            raise ModelLoadError("Registry Version 1 is not READY")
        if not entry.source.startswith("mlflow-artifacts:/"):
            raise ModelLoadError("Registry artifact source must use the MLflow proxy")
    startup.run('model_load', validate_entry)

    # Each attempt has a fresh directory; a partial download is never reused.
    def download():
        temp = TemporaryDirectory(prefix="datacenter-registry-")
        try:
            with registry_transport(startup):
                path = download_artifacts(
                    artifact_uri=f"models:/{name}/{version}", dst_path=temp.name,
                    tracking_uri=uri, registry_uri=uri,
                )
            return temp, path
        except BaseException:
            temp.cleanup()
            raise

    downloaded = []
    def tracked_download():
        result = download()
        downloaded.append(result[0])
        return result
    try:
        # Also clean a successful download rejected because it exceeded the budget.
        temp, path = startup.run('artifact_download', tracked_download, classify_registry)
        return startup.run('model_load', lambda: _load_package(path))
    finally:
        for temp in downloaded:
            temp.cleanup()


def _load_registry(startup=None):
    try:
        return _registry_steps(startup or Startup())
    except StartupError as error:
        raise ModelLoadError(str(error)) from None


def load_model(local_path, startup=None):
    startup = startup or Startup()
    source = os.environ.get("MODEL_SOURCE", "local")
    try:
        if source == "local":
            return startup.run('model_load', lambda: joblib.load(local_path))
        if source != "registry":
            def reject():
                raise ModelLoadError("MODEL_SOURCE must be local or registry")
            startup.run('configuration', reject)
        return _load_registry(startup)
    except Exception as error:
        if not isinstance(error, (StartupError, ModelLoadError)):
            startup._event('model_load', Failure('model_initialization_unknown'), 1, 'permanent_or_unknown')
        # Fixed public text, no upstream exception name/message/traceback or fallback.
        raise ModelLoadError("Model initialization failed; no fallback") from None
