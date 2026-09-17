from pathlib import Path
import json
import hashlib
import io
import os
import platform
import subprocess

import mlflow
import sklearn

import joblib
import matplotlib

# GUI가 없는 EC2에서 그래프를 파일로 저장
matplotlib.use("Agg")

import matplotlib.pyplot as plt
import pandas as pd
from sklearn.ensemble import RandomForestClassifier
from sklearn.metrics import (
    ConfusionMatrixDisplay,
    accuracy_score,
    classification_report,
    confusion_matrix,
    precision_score,
    recall_score,
    f1_score,
)
from sklearn.model_selection import train_test_split

EXPERIMENT_NAME = "datacenter-anomaly-detection"


def git_metadata():
    """Record unavailable explicitly when training outside a Git checkout."""
    root = Path(__file__).resolve().parent
    try:
        sha = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=root, stderr=subprocess.DEVNULL, text=True
        ).strip()
        status = subprocess.check_output(
            ["git", "status", "--porcelain"], cwd=root,
            stderr=subprocess.DEVNULL, text=True
        )
        return {"git_commit_sha": sha, "git_dirty": str(bool(status)).lower()}
    except (OSError, subprocess.CalledProcessError):
        return {"git_commit_sha": "unknown", "git_dirty": "unknown"}


def main():
    tracking_uri = os.environ.get("MLFLOW_TRACKING_URI", "").strip()
    if not tracking_uri:
        raise RuntimeError("Set MLFLOW_TRACKING_URI before training.")
    mlflow.set_tracking_uri(tracking_uri)
    mlflow.set_experiment(EXPERIMENT_NAME)
    # 학습 데이터 읽기
    dataset_path = Path("data/training_data.csv")
    dataset_bytes = dataset_path.read_bytes()
    df = pd.read_csv(io.BytesIO(dataset_bytes))
    features = ["cpu", "memory", "temperature", "power"]

    X = df[features]
    y = df["label"]

    test_size = 0.2
    tags = {
        "synthetic_data": "true",
        "dataset_path": dataset_path.as_posix(),
        "training_data_sha256": hashlib.sha256(dataset_bytes).hexdigest(),
        **git_metadata(),
        "python_version": platform.python_version(),
        "scikit_learn_version": sklearn.__version__,
        "mlflow_version": mlflow.__version__,
    }
    with mlflow.start_run(tags=tags) as run:
        # 정상/이상 비율을 유지하며 학습 80%, 테스트 20%로 분리
        # 현재 데이터는 독립적으로 생성한 합성 데이터이므로 무작위 분리 사용
        X_train, X_test, y_train, y_test = train_test_split(
            X,
            y,
            test_size=test_size,
            stratify=y,
            random_state=42,
        )

        # 테스트 데이터는 학습에 사용하지 않음
        model = RandomForestClassifier(
            n_estimators=100,
            max_depth=10,
            class_weight="balanced",
            random_state=42,
            n_jobs=2,
        )
        mlflow.log_params({
            **{key: model.get_params()[key] for key in (
                "n_estimators", "max_depth", "class_weight", "random_state", "n_jobs"
            )},
            "test_size": test_size,
            "features": json.dumps(features),
            "positive_label": 1,
            "data_rows": len(df),
        })
        model.fit(X_train, y_train)

        # 이상을 양성 클래스인 1로 평가
        y_pred = model.predict(X_test)
        cm = confusion_matrix(y_test, y_pred, labels=[0, 1])
        tn, fp, fn, tp = cm.ravel()

        metrics = {
            "accuracy": float(accuracy_score(y_test, y_pred)),
            "precision": float(precision_score(y_test, y_pred, zero_division=0)),
            "recall": float(recall_score(y_test, y_pred, zero_division=0)),
            "f1": float(f1_score(y_test, y_pred, zero_division=0)),
            "tn": int(tn),
            "fp": int(fp),
            "fn": int(fn),
            "tp": int(tp),
        }

        print(f"Train rows: {len(X_train)}")
        print(f"Test rows: {len(X_test)}")
        print("\nClassification report:")
        print(classification_report(
            y_test,
            y_pred,
            labels=[0, 1],
            target_names=["NORMAL", "ANOMALY"],
            digits=4,
            zero_division=0,
        ))
        print(f"False negatives (missed anomalies): {fn}")
        print(f"Anomaly recall: {metrics['recall']:.4f}")

        # 모델과 평가 결과 저장
        Path("models").mkdir(exist_ok=True)
        Path("reports").mkdir(exist_ok=True)

        joblib.dump(model, "models/model.pkl")

        with open("reports/metrics.json", "w", encoding="utf-8") as f:
            json.dump(metrics, f, indent=2)

        # 혼동행렬의 행은 실제 정답, 열은 모델 예측
        display = ConfusionMatrixDisplay(
            confusion_matrix=cm,
            display_labels=["NORMAL", "ANOMALY"],
        )
        display.plot(cmap="Blues", values_format="d")
        plt.title("RandomForest - Synthetic Test Data")
        plt.tight_layout()
        plt.savefig("reports/confusion_matrix.png", dpi=150)
        plt.close()

        print("\nSaved: models/model.pkl")
        print("Saved: reports/metrics.json")
        print("Saved: reports/confusion_matrix.png")

        mlflow.log_metrics(metrics)
        mlflow.log_artifact("models/model.pkl", artifact_path="models")
        mlflow.log_artifact("reports/metrics.json", artifact_path="reports")
        mlflow.log_artifact("reports/confusion_matrix.png", artifact_path="reports")
        print(f"MLflow run ID: {run.info.run_id}")
        return run.info.run_id


if __name__ == "__main__":
    main()
