"""Small object-store boundary used by the S3 Registry adapter."""

from __future__ import annotations

import hashlib
import logging
import math
import shutil
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Lock
from typing import Any, Protocol

import boto3
from botocore.exceptions import BotoCoreError, ClientError

from ifx_registry.domain.errors import InvalidPublicationError, RegistryUnavailableError

MULTIPART_UPLOAD_THRESHOLD_BYTES = 64 * 1024 * 1024
MINIMUM_MULTIPART_PART_BYTES = 64 * 1024 * 1024
MAXIMUM_MULTIPART_PARTS = 10_000

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ObjectMetadata:
    key: str
    size_bytes: int
    content_type: str | None
    metadata: Mapping[str, str]


class ObjectStore(Protocol):
    bucket: str

    def list_keys(self, prefix: str) -> tuple[str, ...]: ...

    def read_bytes(self, key: str) -> bytes | None: ...

    def stat(self, key: str) -> ObjectMetadata | None: ...

    def download_file(self, key: str, destination: Path) -> None: ...

    def put_file_if_absent(
        self,
        path: Path,
        key: str,
        *,
        content_type: str | None,
        metadata: Mapping[str, str],
    ) -> bool: ...

    def put_bytes_if_absent(self, key: str, payload: bytes, *, content_type: str) -> bool: ...


class Boto3ObjectStore:
    """S3 implementation using YAML-provided or ambient AWS credentials."""

    def __init__(
        self,
        bucket: str,
        *,
        region: str = "us-east-1",
        endpoint_url: str | None = None,
        role_arn: str | None = None,
        external_id: str | None = None,
        role_session_name: str = "ifx-registry",
        access_key_id: str | None = None,
        secret_access_key: str | None = None,
        client: Any | None = None,
    ):
        if not bucket.strip():
            raise ValueError("S3 bucket must not be blank")
        self.bucket = bucket.strip()
        self._s3_client = client
        self._client_expiration: datetime | None = None
        self._client_lock = Lock()
        self._region = region
        self._endpoint_url = endpoint_url
        self._role_arn = role_arn
        self._external_id = external_id
        self._role_session_name = role_session_name
        self._access_key_id = access_key_id
        self._secret_access_key = secret_access_key

    def _client(self) -> Any:
        refresh_at = datetime.now(UTC) + timedelta(minutes=5)
        if self._s3_client is not None and (
            self._client_expiration is None or self._client_expiration > refresh_at
        ):
            return self._s3_client
        with self._client_lock:
            if self._s3_client is None or (
                self._client_expiration is not None and self._client_expiration <= refresh_at
            ):
                self._s3_client, self._client_expiration = self._create_client(
                    region=self._region,
                    endpoint_url=self._endpoint_url,
                    role_arn=self._role_arn,
                    external_id=self._external_id,
                    role_session_name=self._role_session_name,
                    access_key_id=self._access_key_id,
                    secret_access_key=self._secret_access_key,
                )
        return self._s3_client

    @staticmethod
    def _create_client(
        *,
        region: str,
        endpoint_url: str | None,
        role_arn: str | None,
        external_id: str | None,
        role_session_name: str,
        access_key_id: str | None,
        secret_access_key: str | None,
    ) -> tuple[Any, datetime | None]:
        session = boto3.Session(
            aws_access_key_id=access_key_id,
            aws_secret_access_key=secret_access_key,
            region_name=region,
        )
        if not role_arn:
            return session.client("s3", region_name=region, endpoint_url=endpoint_url), None
        assume_role: dict[str, str] = {
            "RoleArn": role_arn,
            "RoleSessionName": role_session_name,
        }
        if external_id:
            assume_role["ExternalId"] = external_id
        try:
            credentials = session.client("sts", region_name=region).assume_role(**assume_role)[
                "Credentials"
            ]
        except (BotoCoreError, ClientError) as error:
            raise RegistryUnavailableError(
                f"Could not assume the configured Registry AWS role: {error}"
            ) from error
        expiration = credentials["Expiration"]
        if isinstance(expiration, str):
            expiration = datetime.fromisoformat(expiration.replace("Z", "+00:00"))
        return (
            boto3.client(
                "s3",
                region_name=region,
                endpoint_url=endpoint_url,
                aws_access_key_id=credentials["AccessKeyId"],
                aws_secret_access_key=credentials["SecretAccessKey"],
                aws_session_token=credentials["SessionToken"],
            ),
            expiration,
        )

    def list_keys(self, prefix: str) -> tuple[str, ...]:
        try:
            paginator = self._client().get_paginator("list_objects_v2")
            keys = [
                item["Key"]
                for page in paginator.paginate(Bucket=self.bucket, Prefix=prefix)
                for item in page.get("Contents", ())
            ]
            return tuple(sorted(keys))
        except (BotoCoreError, ClientError) as error:
            raise self._unavailable("list published manifests", error) from error

    def read_bytes(self, key: str) -> bytes | None:
        try:
            response = self._client().get_object(Bucket=self.bucket, Key=key)
            return bytes(response["Body"].read())
        except ClientError as error:
            if _is_not_found(error):
                return None
            raise self._unavailable(f"read s3://{self.bucket}/{key}", error) from error
        except BotoCoreError as error:
            raise self._unavailable(f"read s3://{self.bucket}/{key}", error) from error

    def stat(self, key: str) -> ObjectMetadata | None:
        try:
            response = self._client().head_object(Bucket=self.bucket, Key=key)
            return ObjectMetadata(
                key=key,
                size_bytes=int(response["ContentLength"]),
                content_type=response.get("ContentType"),
                metadata=dict(response.get("Metadata") or {}),
            )
        except ClientError as error:
            if _is_not_found(error):
                return None
            raise self._unavailable(f"inspect s3://{self.bucket}/{key}", error) from error
        except BotoCoreError as error:
            raise self._unavailable(f"inspect s3://{self.bucket}/{key}", error) from error

    def download_file(self, key: str, destination: Path) -> None:
        try:
            self._client().download_file(self.bucket, key, str(destination))
        except (BotoCoreError, ClientError, OSError) as error:
            raise self._unavailable(f"download s3://{self.bucket}/{key}", error) from error

    def put_file_if_absent(
        self,
        path: Path,
        key: str,
        *,
        content_type: str | None,
        metadata: Mapping[str, str],
    ) -> bool:
        """Upload exact verified bytes without replacing an existing key."""

        try:
            size = path.stat().st_size
        except OSError as error:
            raise self._unavailable(f"read upload file {path}", error) from error
        if size >= MULTIPART_UPLOAD_THRESHOLD_BYTES:
            return self._put_multipart_file_if_absent(
                path,
                key,
                content_type=content_type,
                metadata=metadata,
            )
        return self._put_small_file_if_absent(
            path,
            key,
            content_type=content_type,
            metadata=metadata,
        )

    def _put_small_file_if_absent(
        self,
        path: Path,
        key: str,
        *,
        content_type: str | None,
        metadata: Mapping[str, str],
    ) -> bool:
        arguments: dict[str, Any] = {
            "Bucket": self.bucket,
            "Key": key,
            "Metadata": dict(metadata),
            "IfNoneMatch": "*",
        }
        if content_type:
            arguments["ContentType"] = content_type
        try:
            with tempfile.TemporaryDirectory(prefix="ifx-registry-upload-") as directory:
                staged = Path(directory) / "payload"
                shutil.copyfile(path, staged)
                _verify_expected_sha256(staged, metadata)
                handle = staged.open("rb")
                try:
                    self._client().put_object(Body=handle, **arguments)
                finally:
                    handle.close()
            return True
        except ClientError as error:
            if _error_code(error) in {"PreconditionFailed", "412"}:
                return False
            raise self._unavailable(f"upload s3://{self.bucket}/{key}", error) from error
        except (BotoCoreError, OSError) as error:
            raise self._unavailable(f"upload s3://{self.bucket}/{key}", error) from error

    def _put_multipart_file_if_absent(
        self,
        path: Path,
        key: str,
        *,
        content_type: str | None,
        metadata: Mapping[str, str],
    ) -> bool:
        create_arguments: dict[str, Any] = {
            "Bucket": self.bucket,
            "Key": key,
            "Metadata": dict(metadata),
        }
        if content_type:
            create_arguments["ContentType"] = content_type
        upload_id: str | None = None
        completed = False
        try:
            upload_id = str(
                self._client().create_multipart_upload(**create_arguments)["UploadId"]
            )
            size = path.stat().st_size
            part_size = max(
                MINIMUM_MULTIPART_PART_BYTES,
                math.ceil(size / MAXIMUM_MULTIPART_PARTS),
            )
            parts = []
            digest = hashlib.sha256()
            with path.open("rb") as handle:
                part_number = 1
                while chunk := handle.read(part_size):
                    digest.update(chunk)
                    response = self._client().upload_part(
                        Bucket=self.bucket,
                        Key=key,
                        UploadId=upload_id,
                        PartNumber=part_number,
                        Body=chunk,
                    )
                    parts.append({"PartNumber": part_number, "ETag": response["ETag"]})
                    part_number += 1
            expected = metadata.get("sha256")
            if expected is not None and digest.hexdigest() != expected:
                raise InvalidPublicationError(
                    f"Snapshot file changed while it was being uploaded: {path}"
                )
            self._client().complete_multipart_upload(
                Bucket=self.bucket,
                Key=key,
                UploadId=upload_id,
                MultipartUpload={"Parts": parts},
                IfNoneMatch="*",
            )
            completed = True
            return True
        except ClientError as error:
            if _error_code(error) in {"PreconditionFailed", "412"}:
                return False
            raise self._unavailable(f"upload s3://{self.bucket}/{key}", error) from error
        except (BotoCoreError, OSError) as error:
            raise self._unavailable(f"upload s3://{self.bucket}/{key}", error) from error
        finally:
            if upload_id is not None and not completed:
                try:
                    self._client().abort_multipart_upload(
                        Bucket=self.bucket,
                        Key=key,
                        UploadId=upload_id,
                    )
                except (BotoCoreError, ClientError, RegistryUnavailableError) as abort_error:
                    logger.warning(
                        "Could not abort multipart upload s3://%s/%s (%s): %s",
                        self.bucket,
                        key,
                        upload_id,
                        abort_error,
                    )

    def put_bytes_if_absent(self, key: str, payload: bytes, *, content_type: str) -> bool:
        try:
            self._client().put_object(
                Bucket=self.bucket,
                Key=key,
                Body=payload,
                ContentType=content_type,
                IfNoneMatch="*",
            )
            return True
        except ClientError as error:
            if _error_code(error) in {"PreconditionFailed", "412"}:
                return False
            raise self._unavailable(f"commit s3://{self.bucket}/{key}", error) from error
        except BotoCoreError as error:
            raise self._unavailable(f"commit s3://{self.bucket}/{key}", error) from error

    def _unavailable(self, operation: str, error: Exception) -> RegistryUnavailableError:
        return RegistryUnavailableError(
            f"Could not {operation} in Registry bucket {self.bucket}: {error}"
        )


def _error_code(error: ClientError) -> str:
    return str(error.response.get("Error", {}).get("Code", ""))


def _is_not_found(error: ClientError) -> bool:
    return _error_code(error) in {"404", "NoSuchKey", "NotFound"}


def _verify_expected_sha256(path: Path, metadata: Mapping[str, str]) -> None:
    expected = metadata.get("sha256")
    if expected is None:
        return
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    if digest.hexdigest() != expected:
        raise InvalidPublicationError(
            f"Snapshot file changed while it was being staged for upload: {path}"
        )
