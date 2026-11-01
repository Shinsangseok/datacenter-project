"""Training integration contracts using synthetic files and mocked MLflow calls.

All outputs live in a temporary directory. No Tracking Server, Registry, DB,
Kubernetes or AWS operations are permitted by this suite.
"""
from contextlib import ExitStack, redirect_stderr, redirect_stdout
import hashlib
import io
import json
import os
from pathlib import Path
import shlex
import socket
import subprocess
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import pandas as pd
import yaml

import train_model as training

ROOT = Path(__file__).resolve().parents[1]


class TrainingTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.dict(os.environ, {}, clear=True))
        self.stack.enter_context(patch.object(
            socket.socket, "connect", side_effect=AssertionError("network forbidden")))
        self.calls = {}
        for name in ("set_tracking_uri", "set_experiment", "start_run", "log_params",
                     "log_metrics", "log_artifact", "register_model"):
            self.calls[name] = self.stack.enter_context(patch.object(training.mlflow, name))
        self.calls["start_run"].return_value.__enter__.return_value = SimpleNamespace(
            info=SimpleNamespace(run_id="synthetic-run-id"))

    def test_missing_tracking_uri_fails_before_any_tracking_or_training(self):
        for value in (None, "", "   "):
            with self.subTest(value=value), patch.object(training, "RandomForestClassifier") as model:
                if value is None:
                    os.environ.pop("MLFLOW_TRACKING_URI", None)
                else:
                    os.environ["MLFLOW_TRACKING_URI"] = value
                with self.assertRaisesRegex(RuntimeError, "Set MLFLOW_TRACKING_URI"):
                    training.main([])
                model.assert_not_called()
        for call in self.calls.values():
            call.assert_not_called()

    def test_invalid_cli_fails_before_tracking(self):
        cases = [("--n-estimators", "0"), ("--max-depth", "0"),
                 ("--test-size", "0"), ("--test-size", "1"),
                 ("--test-size", "nan"), ("--n-jobs", "0"),
                 ("--random-state", "-1"), ("--random-state", str(2**32)),
                 ("--class-weight", "unsupported")]
        for arguments in cases:
            with self.subTest(arguments=arguments), redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as raised:
                    training.main(list(arguments))
                self.assertEqual(raised.exception.code, 2)
        for call in self.calls.values():
            call.assert_not_called()

    def test_projects_defaults_and_command_match_direct_cli(self):
        project = yaml.safe_load((ROOT / "MLproject").read_text())
        entry = project["entry_points"]["train"]
        parameters = {name: definition["default"]
                      for name, definition in entry["parameters"].items()}
        self.assertEqual(parameters, vars(training.parse_args([])))
        command = shlex.split(entry["command"].format(**parameters))
        self.assertEqual(command[:2], ["python", "train_model.py"])
        self.assertEqual(vars(training.parse_args(command[2:])), parameters)
        self.assertNotIn("MLFLOW_TRACKING_URI", project)

    def test_training_logs_exact_data_contract_and_three_artifacts(self):
        with TemporaryDirectory() as directory:
            previous = Path.cwd()
            os.chdir(directory)
            try:
                Path("data").mkdir()
                data = pd.DataFrame({
                    "cpu": list(range(40)), "memory": list(range(40, 80)),
                    "temperature": [20 + i % 4 for i in range(40)],
                    "power": [100 + i for i in range(40)],
                    "label": [i % 2 for i in range(40)],
                })
                dataset = Path("data/training_data.csv")
                data.to_csv(dataset, index=False)
                digest = hashlib.sha256(dataset.read_bytes()).hexdigest()
                os.environ["MLFLOW_TRACKING_URI"] = "https://tracking.example.invalid"
                with patch.object(training, "git_metadata", return_value={
                        "git_commit_sha": "fixture-commit", "git_dirty": "false"}), \
                        redirect_stdout(io.StringIO()):
                    run_id = training.main(["--n-estimators", "3", "--max-depth", "2",
                                            "--n-jobs", "1", "--test-size", "0.25"])
                self.assertEqual(run_id, "synthetic-run-id")
                self.calls["set_tracking_uri"].assert_called_once_with(
                    "https://tracking.example.invalid")
                self.calls["set_experiment"].assert_called_once_with(training.EXPERIMENT_NAME)
                self.calls["start_run"].assert_called_once()
                tags = self.calls["start_run"].call_args.kwargs["tags"]
                self.assertEqual(tags["training_data_sha256"], digest)
                self.assertEqual(tags["synthetic_data"], "true")
                self.assertEqual(tags["git_commit_sha"], "fixture-commit")
                parameters = self.calls["log_params"].call_args.args[0]
                self.assertEqual(parameters["n_estimators"], 3)
                self.assertEqual(parameters["max_depth"], 2)
                self.assertEqual(parameters["test_size"], 0.25)
                self.assertEqual(parameters["positive_label"], 1)
                self.assertEqual(parameters["data_rows"], 40)
                self.assertEqual(json.loads(parameters["features"]),
                                 ["cpu", "memory", "temperature", "power"])
                metrics = self.calls["log_metrics"].call_args.args[0]
                self.assertEqual(set(metrics), {"accuracy", "precision", "recall", "f1",
                                                "tn", "fp", "fn", "tp"})
                self.assertEqual(sum(metrics[key] for key in ("tn", "fp", "fn", "tp")), 10)
                self.assertEqual(json.loads(Path("reports/metrics.json").read_text()), metrics)
                artifacts = {call.args[0]: call.kwargs["artifact_path"]
                             for call in self.calls["log_artifact"].call_args_list}
                self.assertEqual(artifacts, {"models/model.pkl": "models",
                    "reports/metrics.json": "reports", "reports/confusion_matrix.png": "reports"})
                for artifact in artifacts:
                    self.assertGreater(Path(artifact).stat().st_size, 0)
                self.calls["register_model"].assert_not_called()
            finally:
                os.chdir(previous)

    def test_git_metadata_without_git_is_explicitly_unknown(self):
        for error in (OSError(), subprocess.CalledProcessError(1, ["git"])):
            with self.subTest(error=type(error).__name__), \
                    patch.object(training.subprocess, "check_output", side_effect=error):
                self.assertEqual(training.git_metadata(),
                                 {"git_commit_sha": "unknown", "git_dirty": "unknown"})


if __name__ == "__main__":
    unittest.main()
