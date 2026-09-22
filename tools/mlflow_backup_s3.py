"""Versioned S3 SSE-KMS transport for already-created MLflow logical dumps.

No DB operations, raw encryption keys, multipart, or AWS provisioning. Requires
boto3 >= 1.34.46 in the backup worker (not FastAPI). Bucket Key is always ON.

Required environment: AWS_REGION, MLFLOW_BACKUP_BUCKET, MLFLOW_BACKUP_PREFIX,
MLFLOW_BACKUP_KMS_KEY_ID (immutable key ID or key ARN, never an alias).
Optional: MLFLOW_BACKUP_EXPECTED_BUCKET_OWNER (12-digit account ID).
AWS credentials must come from an IAM role/SSO provider, not static keys.
AssumeRole source profiles must themselves use roles/SSO, never static keys;
validate the role trust/source chain when provisioning the worker.

    python tools/mlflow_backup_s3.py backup --dump /secure/dump.sql \
        --timestamp 2026-09-22T12:00:00Z --manifest /secure/backup.json
    python tools/mlflow_backup_s3.py restore --manifest /secure/backup.json \
        --bucket BUCKET --key EXACT_KEY --version-id EXACT_VERSION \
        --output /secure/restored.sql

Store the manifest outside Git/cluster with controlled access: it is the trusted
checksum/version reference, not a signed attestation. ETag is opaque. SHA-256 is
only a fingerprint of dump bytes, not a semantic schema/data fingerprint. The
UTC timestamp identifies this backup; it must be supplied by the dump workflow.
Repeat inputs yield the same key; Versioning preserves each upload separately.
For old backups configure their original key ID/ARN, not today's replacement.

Default size cap is 64 MiB; --max-bytes can change it within single PUT limits.
Restore stages plaintext in a private file beside the output and publishes it
only after validation. Use a secured directory/encrypted volume or tmpfs. Never
pipe a partial download into mysql. Existing outputs/manifests are not replaced.
Writer IAM: PutObject + GenerateDataKey. Reader: GetObjectVersion + Decrypt.
Bucket Key ON requires bucket ARN KMS encryption-context policy conditions;
actual AWS IAM behavior still needs an isolated integration check.
"""
from __future__ import annotations

import argparse
import base64
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import sys
import tempfile
from typing import Mapping

FORMAT = "mlflow-sql-s3-sse-kms-v1"
DEFAULT_MAX_BYTES = 64 * 1024 * 1024
SINGLE_PUT_MAX_BYTES = 5 * 1024**3
MANIFEST_MAX_BYTES = 64 * 1024
CHUNK_BYTES = 1024 * 1024
KEY_ID = r"(?:[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}|mrk-[0-9a-f]{32})"
KEY_ARN = re.compile(r"arn:aws:kms:([a-z0-9-]+):[0-9]{12}:key/(" + KEY_ID + r")")
ROLE_PROVIDERS = frozenset({"assume-role", "assume-role-with-web-identity",
                            "iam-role", "container-role", "sso"})


class BackupError(Exception):
    """Only fixed, credential-free messages may cross this tool's boundary."""


def require(condition: bool, message: str) -> None:
    if not condition:
        raise BackupError(message)


@dataclass(frozen=True)
class Config:
    region: str
    bucket: str
    prefix: str
    kms_key_id: str
    expected_bucket_owner: str | None = None

    def __post_init__(self):
        require(bool(re.fullmatch(r"[a-z]{2}(?:-[a-z]+)+-[0-9]+", self.region)),
                "Invalid AWS region.")
        require(bool(re.fullmatch(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]", self.bucket)),
                "Invalid backup bucket.")
        require(bool(re.fullmatch(r"[A-Za-z0-9/_-]+", self.prefix))
                and all(part not in {"", ".", ".."} for part in self.prefix.split("/"))
                and len(self.prefix) <= 700, "Invalid backup prefix.")
        arn = KEY_ARN.fullmatch(self.kms_key_id)
        require(bool(arn or re.fullmatch(KEY_ID, self.kms_key_id)),
                "KMS key must be an immutable key ID or key ARN.")
        require(not arn or arn.group(1) == self.region, "KMS key region mismatch.")
        require(self.expected_bucket_owner is None or bool(re.fullmatch(
            r"[0-9]{12}", self.expected_bucket_owner)), "Invalid expected bucket owner.")

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Config:
        env = os.environ if env is None else env
        names = ("AWS_REGION", "MLFLOW_BACKUP_BUCKET", "MLFLOW_BACKUP_PREFIX",
                 "MLFLOW_BACKUP_KMS_KEY_ID")
        for name in names:
            require(bool(env.get(name, "").strip()), "Missing required environment variable: " + name)
        return cls(*(env[name].strip() for name in names),
                   expected_bucket_owner=env.get("MLFLOW_BACKUP_EXPECTED_BUCKET_OWNER"))

    def owner_args(self) -> dict:
        return ({"ExpectedBucketOwner": self.expected_bucket_owner}
                if self.expected_bucket_owner else {})


def create_s3_client(config: Config):
    """Resolve role credentials without fetching, displaying or copying key values."""
    try:
        import boto3
        session = boto3.Session(region_name=config.region)
        credentials = session.get_credentials()
        require(credentials is not None and credentials.method in ROLE_PROVIDERS,
                "An IAM role or SSO credential provider is required.")
        return session.client("s3")
    except BackupError:
        raise
    except Exception:
        raise BackupError("AWS client initialization failed.") from None


def _limit(max_bytes: int) -> None:
    require(type(max_bytes) is int and 0 < max_bytes <= SINGLE_PUT_MAX_BYTES,
            "Invalid single-upload size limit.")


def _timestamp(value: str) -> datetime:
    try:
        require(isinstance(value, str) and value.endswith("Z"), "Timestamp must be UTC with Z suffix.")
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
        require(parsed.utcoffset().total_seconds() == 0, "Timestamp must be UTC.")
        return parsed.astimezone(timezone.utc)
    except (TypeError, ValueError, AttributeError):
        raise BackupError("Invalid UTC timestamp.") from None


def object_key(config: Config, timestamp: str, sha256: str) -> str:
    require(isinstance(sha256, str) and bool(re.fullmatch(r"[0-9a-f]{64}", sha256)),
            "Invalid SHA-256 fingerprint.")
    stamp = _timestamp(timestamp)
    return f"{config.prefix}/{stamp:%Y/%m/%d}/{stamp:%Y%m%dT%H%M%S%fZ}-{sha256}.sql"


def _key_matches(config: Config, arn: str) -> bool:
    match = KEY_ARN.fullmatch(arn) if isinstance(arn, str) else None
    return bool(match and match.group(1) == config.region and
                (arn == config.kms_key_id or match.group(2) == config.kms_key_id))


def _version(value) -> bool:
    return isinstance(value, str) and bool(value.strip()) and value.lower() != "null"


def _encryption(config: Config, response: dict) -> None:
    require(response.get("ServerSideEncryption") == "aws:kms"
            and response.get("BucketKeyEnabled") is True
            and _key_matches(config, response.get("SSEKMSKeyId")),
            "S3 encryption response mismatch.")


def _call(client, operation: str, **kwargs):
    try:
        return getattr(client, operation)(**kwargs)
    except Exception:
        raise BackupError("S3 request failed.") from None


def backup(client, config: Config, dump: Path, timestamp: str,
           max_bytes: int = DEFAULT_MAX_BYTES) -> dict:
    """Upload bytes once; return a manifest only after version/encryption checks."""
    _limit(max_bytes)
    stamp = _timestamp(timestamp).isoformat().replace("+00:00", "Z")
    try:
        with Path(dump).open("rb") as source:
            payload = source.read(max_bytes + 1)
    except OSError:
        raise BackupError("Cannot read logical dump.") from None
    require(0 < len(payload) <= max_bytes, "Logical dump is empty or exceeds size limit.")
    digest = hashlib.sha256(payload).hexdigest()
    checksum = base64.b64encode(bytes.fromhex(digest)).decode("ascii")
    key = object_key(config, stamp, digest)
    result = _call(client, "put_object", Bucket=config.bucket, Key=key, Body=payload,
                   ContentType="application/sql", ServerSideEncryption="aws:kms",
                   SSEKMSKeyId=config.kms_key_id, BucketKeyEnabled=True,
                   ChecksumAlgorithm="SHA256", ChecksumSHA256=checksum,
                   **config.owner_args())
    _encryption(config, result)
    require(_version(result.get("VersionId")), "S3 did not return a valid VersionId.")
    require(isinstance(result.get("ETag"), str) and bool(result["ETag"]), "S3 did not return an ETag.")
    require(result.get("ChecksumSHA256") == checksum, "S3 upload checksum mismatch.")
    return {"format": FORMAT, "region": config.region, "bucket": config.bucket,
            "key": key, "version_id": result["VersionId"], "etag": result["ETag"],
            "sha256": digest, "size_bytes": len(payload), "timestamp": stamp,
            "uploaded_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "fingerprint_scope": "logical-dump-bytes",
            "server_side_encryption": "aws:kms", "kms_key_arn": result["SSEKMSKeyId"],
            "bucket_key_enabled": True}


def _validate_manifest(config: Config, manifest: dict, bucket: str,
                       key: str, version_id: str, max_bytes: int) -> None:
    require(isinstance(manifest, dict), "Invalid backup manifest.")
    require(manifest.get("format") == FORMAT and manifest.get("region") == config.region
            and manifest.get("fingerprint_scope") == "logical-dump-bytes",
            "Unsupported backup manifest.")
    require(bucket == config.bucket == manifest.get("bucket")
            and key == manifest.get("key") and _version(version_id)
            and version_id == manifest.get("version_id"), "Backup reference mismatch.")
    require(manifest.get("server_side_encryption") == "aws:kms"
            and manifest.get("bucket_key_enabled") is True
            and _key_matches(config, manifest.get("kms_key_arn")),
            "Manifest encryption mismatch.")
    require(type(manifest.get("size_bytes")) is int and 0 < manifest["size_bytes"] <= max_bytes,
            "Invalid manifest size.")
    require(isinstance(manifest.get("etag"), str) and bool(manifest["etag"]), "Invalid manifest ETag.")
    require(key == object_key(config, manifest.get("timestamp"), manifest.get("sha256")),
            "Manifest object key mismatch.")


def restore(client, config: Config, manifest: dict, *, bucket: str, key: str,
            version_id: str, output: Path, max_bytes: int = DEFAULT_MAX_BYTES) -> None:
    """Publish a verified plaintext file only. Never connect to or modify a DB."""
    _limit(max_bytes)
    _validate_manifest(config, manifest, bucket, key, version_id, max_bytes)
    output = Path(output)
    require(not output.exists() and not output.is_symlink(), "Output already exists.")
    result = _call(client, "get_object", Bucket=bucket, Key=key, VersionId=version_id,
                   **config.owner_args())
    body = result.get("Body")
    temporary = None
    try:
        require(body is not None, "S3 returned no body.")
        _encryption(config, result)
        require(result.get("VersionId") == version_id
                and result.get("SSEKMSKeyId") == manifest["kms_key_arn"]
                and result.get("ETag") == manifest["etag"]
                and result.get("ContentLength") == manifest["size_bytes"],
                "Downloaded object metadata mismatch.")
        digest = hashlib.sha256()
        size = 0
        with tempfile.NamedTemporaryFile(mode="wb", prefix=".mlflow-restore-",
                                         dir=output.parent, delete=False) as target:
            temporary = Path(target.name)  # tempfile is private (0600).
            while True:
                chunk = body.read(min(CHUNK_BYTES, manifest["size_bytes"] - size + 1))
                if not chunk:
                    break
                size += len(chunk)
                require(size <= manifest["size_bytes"], "Download exceeds manifest size.")
                digest.update(chunk)
                target.write(chunk)
            require(size == manifest["size_bytes"] and hmac.compare_digest(
                digest.hexdigest(), manifest["sha256"]), "Download checksum mismatch.")
            target.flush()
            os.fsync(target.fileno())
        # Atomic create without overwriting an existing path, including symlinks.
        os.link(temporary, output)
    except BackupError:
        raise
    except Exception:
        raise BackupError("Restore download or output failed.") from None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
        if body is not None:
            try:
                body.close()
            except Exception:
                pass


def _read_manifest(path: Path) -> dict:
    try:
        with path.open("rb") as source:
            raw = source.read(MANIFEST_MAX_BYTES + 1)
        require(len(raw) <= MANIFEST_MAX_BYTES, "Manifest exceeds size limit.")
        return json.loads(raw)
    except (OSError, ValueError):
        raise BackupError("Cannot read backup manifest.") from None


def _save_manifest(path: Path, manifest: dict) -> None:
    """Keep an incomplete manifest private; publish without replacing any file."""
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix=".mlflow-manifest-", delete=False) as target:
            temporary = Path(target.name)
            json.dump(manifest, target, indent=2, sort_keys=True)
            target.write("\n")
            target.flush()
            os.fsync(target.fileno())
        os.link(temporary, path)
    except Exception:
        raise BackupError("Manifest save failed; uploaded object may exist. Do not assume backup completion.") from None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    put = commands.add_parser("backup")
    put.add_argument("--dump", type=Path, required=True)
    put.add_argument("--timestamp", required=True)
    put.add_argument("--manifest", type=Path, required=True)
    get = commands.add_parser("restore")
    get.add_argument("--manifest", type=Path, required=True)
    get.add_argument("--bucket", required=True)
    get.add_argument("--key", required=True)
    get.add_argument("--version-id", required=True)
    get.add_argument("--output", type=Path, required=True)
    for command in (put, get):
        command.add_argument("--max-bytes", type=int, default=DEFAULT_MAX_BYTES)
    args = parser.parse_args(argv)
    try:
        config = Config.from_env()
        _limit(args.max_bytes)
        if args.command == "backup":
            require(not args.manifest.exists() and not args.manifest.is_symlink(),
                    "Manifest already exists.")
            result = backup(create_s3_client(config), config, args.dump, args.timestamp, args.max_bytes)
            _save_manifest(args.manifest, result)
        else:
            manifest = _read_manifest(args.manifest)
            _validate_manifest(config, manifest, args.bucket, args.key, args.version_id, args.max_bytes)
            restore(create_s3_client(config), config, manifest, bucket=args.bucket,
                    key=args.key, version_id=args.version_id, output=args.output, max_bytes=args.max_bytes)
        print("Backup manifest saved." if args.command == "backup" else "Verified dump saved; DB restore not executed.")
        return 0
    except BackupError as error:
        print(str(error), file=sys.stderr)
        return 1
    except Exception:
        # Never print SDK exceptions, dump data, paths, env values or tracebacks.
        print("Backup/restore operation failed.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
