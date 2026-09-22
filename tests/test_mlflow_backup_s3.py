"""Offline S3 transport tests. All clients/providers are fakes; networking is blocked."""
import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import socket
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from tools import mlflow_backup_s3 as tool


REGION = "ap-northeast-2"
KEY_ID = "00000000-0000-0000-0000-000000000001"
KEY_ARN = f"arn:aws:kms:{REGION}:000000000000:key/{KEY_ID}"
ENV = {"AWS_REGION": REGION, "MLFLOW_BACKUP_BUCKET": "example-backup-bucket",
       "MLFLOW_BACKUP_PREFIX": "prod/auth", "MLFLOW_BACKUP_KMS_KEY_ID": KEY_ARN}
STAMP = "2026-09-22T12:34:56Z"
PAYLOAD = b"-- synthetic logical dump\nSELECT 1;\n"


class FakeS3:
    """Only PutObject/GetObject exist: list, multipart and DB calls cannot succeed."""
    def __init__(self):
        self.calls = []
        self.objects = {}
        self.last_body = None

    def put_object(self, **kwargs):
        self.calls.append(("put_object", kwargs))
        version = f"version-{len(self.objects) + 1}"
        result = {"VersionId": version, "ETag": '"opaque-etag-not-a-sha256"',
                  "ServerSideEncryption": kwargs["ServerSideEncryption"],
                  "SSEKMSKeyId": KEY_ARN, "BucketKeyEnabled": kwargs["BucketKeyEnabled"],
                  "ChecksumSHA256": kwargs["ChecksumSHA256"]}
        self.objects[(kwargs["Bucket"], kwargs["Key"], version)] = (
            kwargs["Body"], result.copy())
        return result

    def get_object(self, **kwargs):
        self.calls.append(("get_object", kwargs))
        payload, metadata = self.objects[(kwargs["Bucket"], kwargs["Key"], kwargs["VersionId"])]
        self.last_body = io.BytesIO(payload)
        return {**metadata, "Body": self.last_body, "ContentLength": len(payload)}


class BackupTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, ENV, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)
        self.network = patch.object(socket.socket, "connect", side_effect=AssertionError("Network forbidden"))
        self.network.start()
        self.addCleanup(self.network.stop)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.dump = self.root / "dump.sql"
        self.dump.write_bytes(PAYLOAD)
        self.output = self.root / "restored.sql"
        self.config = tool.Config.from_env()
        self.s3 = FakeS3()

    def upload(self):
        return tool.backup(self.s3, self.config, self.dump, STAMP)

    def restore(self, manifest, **overrides):
        args = dict(bucket=manifest["bucket"], key=manifest["key"],
                    version_id=manifest["version_id"], output=self.output)
        args.update(overrides)
        tool.restore(self.s3, self.config, manifest, **args)

    def test_backup_manifest_and_explicit_encryption(self):
        manifest = self.upload()
        operation, call = self.s3.calls[0]
        self.assertEqual(operation, "put_object")
        self.assertEqual(call["ServerSideEncryption"], "aws:kms")
        self.assertEqual(call["SSEKMSKeyId"], KEY_ARN)
        self.assertIs(call["BucketKeyEnabled"], True)
        self.assertEqual(call["ChecksumAlgorithm"], "SHA256")
        self.assertEqual(manifest["version_id"], "version-1")
        self.assertEqual(manifest["sha256"], hashlib.sha256(PAYLOAD).hexdigest())
        self.assertEqual(manifest["size_bytes"], len(PAYLOAD))
        self.assertEqual(manifest["timestamp"], STAMP)
        self.assertTrue(manifest["uploaded_at"].endswith("Z"))
        self.assertEqual(manifest["fingerprint_scope"], "logical-dump-bytes")
        self.assertEqual(manifest["kms_key_arn"], KEY_ARN)
        self.assertNotEqual(manifest["etag"], manifest["sha256"])
        self.assertEqual(len(self.s3.calls), 1)

    def test_deterministic_key_and_separate_versions(self):
        first, second = self.upload(), self.upload()
        self.assertEqual(first["key"], second["key"])
        self.assertNotEqual(first["version_id"], second["version_id"])
        self.assertIn("prod/auth/2026/09/22/20260922T123456000000Z-", first["key"])
        self.dump.write_bytes(b"different dump")
        third = self.upload()
        self.assertNotEqual(first["key"], third["key"])
        # Request the old version after newer objects/versions exist.
        self.restore(first)
        self.assertEqual(self.output.read_bytes(), PAYLOAD)
        self.assertEqual(self.s3.calls[-1][1]["VersionId"], first["version_id"])

    def test_restore_uses_exact_version_and_private_output(self):
        manifest = self.upload()
        self.restore(manifest)
        self.assertEqual(self.s3.calls[-1], ("get_object", {
            "Bucket": manifest["bucket"], "Key": manifest["key"], "VersionId": manifest["version_id"]}))
        self.assertEqual(self.output.read_bytes(), PAYLOAD)
        self.assertEqual(self.output.stat().st_mode & 0o777, 0o600)
        self.assertTrue(self.s3.last_body.closed)
        self.assertFalse(list(self.root.glob(".mlflow-restore-*")))

    def test_checksum_mismatch_never_publishes_output(self):
        manifest = self.upload()
        location = (manifest["bucket"], manifest["key"], manifest["version_id"])
        _, meta = self.s3.objects[location]
        self.s3.objects[location] = (b"X" * len(PAYLOAD), meta)
        with self.assertRaisesRegex(tool.BackupError, "checksum"):
            self.restore(manifest)
        self.assertFalse(self.output.exists())
        self.assertFalse(list(self.root.glob(".mlflow-restore-*")))
        self.assertTrue(self.s3.last_body.closed)

    def test_wrong_reference_fails_before_download(self):
        manifest = self.upload()
        for args in [{"bucket": "other-bucket"}, {"key": "other/key"},
                     {"version_id": "other-version"}, {"version_id": ""}, {"version_id": "null"}]:
            with self.subTest(args=args), self.assertRaises(tool.BackupError):
                self.restore(manifest, **args)
        self.assertEqual(len(self.s3.calls), 1)

    def test_manifest_changes_fail_before_download(self):
        manifest = self.upload()
        bad_values = {"format": "unknown", "sha256": "0" * 64, "size_bytes": -1,
                      "kms_key_arn": KEY_ARN + "bad", "bucket_key_enabled": False,
                      "timestamp": "invalid", "region": "other", "etag": None}
        for field, value in bad_values.items():
            with self.subTest(field=field), self.assertRaises(tool.BackupError):
                self.restore({**manifest, field: value})
        self.assertEqual(len(self.s3.calls), 1)

    def test_invalid_upload_response_has_no_success_manifest(self):
        for field, value in [("VersionId", None), ("VersionId", "null"), ("ETag", None),
                             ("ChecksumSHA256", "bad"), ("BucketKeyEnabled", False),
                             ("ServerSideEncryption", "AES256"), ("SSEKMSKeyId", "unknown")]:
            with self.subTest(field=field):
                client = FakeS3()
                put = client.put_object
                with patch.object(client, "put_object", side_effect=lambda **kw: {**put(**kw), field: value}):
                    with self.assertRaises(tool.BackupError):
                        tool.backup(client, self.config, self.dump, STAMP)

    def test_bad_download_metadata_closes_body_without_output(self):
        manifest = self.upload()
        for field, value in [("VersionId", "wrong"), ("ETag", "wrong"), ("ContentLength", 0),
                             ("BucketKeyEnabled", False), ("SSEKMSKeyId", "wrong")]:
            with self.subTest(field=field):
                get = self.s3.get_object
                with patch.object(self.s3, "get_object", side_effect=lambda **kw: {**get(**kw), field: value}):
                    with self.assertRaises(tool.BackupError):
                        self.restore(manifest)
                self.assertFalse(self.output.exists())
                self.assertTrue(self.s3.last_body.closed)

    def test_short_long_and_interrupted_streams_fail_closed(self):
        manifest = self.upload()
        for payload in [PAYLOAD[:-1], PAYLOAD + b"X"]:
            body = io.BytesIO(payload)
            get = self.s3.get_object
            with patch.object(self.s3, "get_object", side_effect=lambda **kw: {**get(**kw), "Body": body}):
                with self.assertRaises(tool.BackupError):
                    self.restore(manifest)
            self.assertTrue(body.closed)
            self.assertFalse(self.output.exists())
        body = Mock()
        body.read.side_effect = [PAYLOAD[:3], RuntimeError("sensitive stream detail")]
        get = self.s3.get_object
        with patch.object(self.s3, "get_object", side_effect=lambda **kw: {**get(**kw), "Body": body}):
            with self.assertRaises(tool.BackupError) as error:
                self.restore(manifest)
        self.assertNotIn("sensitive", str(error.exception))
        body.close.assert_called_once()
        self.assertFalse(self.output.exists())
        self.assertFalse(list(self.root.glob(".mlflow-restore-*")))

    def test_existing_output_is_not_overwritten(self):
        manifest = self.upload()
        self.output.write_bytes(b"keep")
        with self.assertRaises(tool.BackupError):
            self.restore(manifest)
        self.assertEqual(self.output.read_bytes(), b"keep")
        self.assertEqual(len(self.s3.calls), 1)

    def test_missing_required_environment_fails(self):
        for name in ENV:
            with self.subTest(name=name):
                env = dict(ENV)
                del env[name]
                with self.assertRaisesRegex(tool.BackupError, name):
                    tool.Config.from_env(env)

    def test_alias_invalid_prefix_and_region_mismatch_rejected(self):
        for name, value in [("MLFLOW_BACKUP_KMS_KEY_ID", "alias/backup"),
                            ("MLFLOW_BACKUP_PREFIX", "prod/../auth"),
                            ("MLFLOW_BACKUP_PREFIX", "prod//auth"),
                            ("MLFLOW_BACKUP_KMS_KEY_ID", KEY_ARN.replace(REGION, "us-east-1"))]:
            with self.subTest(name=name), self.assertRaises(tool.BackupError):
                tool.Config.from_env({**ENV, name: value})

    def test_key_id_input_records_resolved_arn(self):
        config = tool.Config.from_env({**ENV, "MLFLOW_BACKUP_KMS_KEY_ID": KEY_ID})
        manifest = tool.backup(self.s3, config, self.dump, STAMP)
        self.assertEqual(self.s3.calls[0][1]["SSEKMSKeyId"], KEY_ID)
        self.assertEqual(manifest["kms_key_arn"], KEY_ARN)
        self.restore(manifest)

    def test_expected_bucket_owner_passed_both_directions(self):
        self.config = tool.Config.from_env({**ENV, "MLFLOW_BACKUP_EXPECTED_BUCKET_OWNER": "000000000000"})
        manifest = self.upload()
        self.restore(manifest)
        for _, kwargs in self.s3.calls:
            self.assertEqual(kwargs["ExpectedBucketOwner"], "000000000000")

    def test_size_limits_and_empty_dump_do_not_upload(self):
        for limit in [0, -1, 1, tool.SINGLE_PUT_MAX_BYTES + 1]:
            with self.subTest(limit=limit), self.assertRaises(tool.BackupError):
                tool.backup(self.s3, self.config, self.dump, STAMP, max_bytes=limit)
        self.dump.write_bytes(b"")
        with self.assertRaises(tool.BackupError):
            self.upload()
        self.assertEqual(self.s3.calls, [])

    def test_restore_limit_checked_before_get(self):
        manifest = self.upload()
        with self.assertRaises(tool.BackupError):
            self.restore(manifest, max_bytes=1)
        self.assertEqual(len(self.s3.calls), 1)

    def test_missing_explicit_version_is_cli_error(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit), patch.object(tool, "create_s3_client") as client:
            tool.main(["restore", "--manifest", "unused", "--bucket", "bucket",
                       "--key", "key", "--output", "unused"])
        client.assert_not_called()

    def test_cli_round_trip_manifest_private_and_no_dump_logging(self):
        manifest_path = self.root / "manifest.json"
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch.object(tool, "create_s3_client", return_value=self.s3), contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            result = tool.main(["backup", "--dump", str(self.dump), "--timestamp", STAMP,
                                "--manifest", str(manifest_path)])
            self.assertEqual(result, 0)
            manifest = json.loads(manifest_path.read_text())
            result = tool.main(["restore", "--manifest", str(manifest_path),
                                "--bucket", manifest["bucket"], "--key", manifest["key"],
                                "--version-id", manifest["version_id"], "--output", str(self.output)])
            self.assertEqual(result, 0)
        self.assertEqual(manifest_path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.output.read_bytes(), PAYLOAD)
        self.assertNotIn("SELECT", stdout.getvalue() + stderr.getvalue())
        self.assertNotIn(KEY_ARN, stdout.getvalue() + stderr.getvalue())

    def test_sdk_exception_details_never_reach_cli_or_manifest(self):
        client = Mock()
        marker = "SENSITIVE_TEST_SENTINEL"
        client.put_object.side_effect = RuntimeError(marker)
        manifest = self.root / "manifest.json"
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch.object(tool, "create_s3_client", return_value=client), contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            rc = tool.main(["backup", "--dump", str(self.dump), "--timestamp", STAMP, "--manifest", str(manifest)])
        self.assertEqual(rc, 1)
        self.assertNotIn(marker, stdout.getvalue() + stderr.getvalue())
        self.assertFalse(manifest.exists())

    def test_existing_manifest_blocks_upload(self):
        manifest = self.root / "manifest.json"
        manifest.write_text("keep")
        with patch.object(tool, "create_s3_client") as client, contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(tool.main(["backup", "--dump", str(self.dump), "--timestamp", STAMP,
                                       "--manifest", str(manifest)]), 1)
        client.assert_not_called()
        self.assertEqual(manifest.read_text(), "keep")

    def test_credential_providers_allow_roles_reject_static_and_missing(self):
        for method in sorted(tool.ROLE_PROVIDERS) + ["env", "shared-credentials-file", "config-file", "explicit", None]:
            with self.subTest(method=method):
                session = Mock()
                session.get_credentials.return_value = (SimpleNamespace(method=method) if method else None)
                fake_boto3 = SimpleNamespace(Session=Mock(return_value=session))
                with patch.dict("sys.modules", {"boto3": fake_boto3}):
                    if method in tool.ROLE_PROVIDERS:
                        self.assertIs(tool.create_s3_client(self.config), session.client.return_value)
                        session.client.assert_called_once_with("s3")
                    else:
                        with self.assertRaises(tool.BackupError):
                            tool.create_s3_client(self.config)
                        session.client.assert_not_called()
                fake_boto3.Session.assert_called_once_with(region_name=REGION)

    def test_credential_provider_failure_is_sanitized(self):
        session = Mock()
        session.get_credentials.side_effect = RuntimeError("SENSITIVE_TEST_SENTINEL")
        with patch.dict("sys.modules", {"boto3": SimpleNamespace(Session=Mock(return_value=session))}):
            with self.assertRaises(tool.BackupError) as error:
                tool.create_s3_client(self.config)
        self.assertNotIn("SENSITIVE_TEST_SENTINEL", str(error.exception))
        session.client.assert_not_called()


if __name__ == "__main__":
    unittest.main()
