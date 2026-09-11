"""Contract-level tests for boto3 object-store behavior."""

import hashlib
from io import BytesIO
from pathlib import Path
from typing import Any

import pytest
from botocore.exceptions import ClientError

import ifx_registry.infrastructure.object_store as object_store_module
from ifx_registry.domain.errors import InvalidPublicationError
from ifx_registry.infrastructure.object_store import Boto3ObjectStore


def _client_error(code: str, operation: str) -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": code}}, operation)


class FakePaginator:
    def paginate(self, **arguments: Any) -> list[dict[str, Any]]:
        assert arguments == {"Bucket": "bucket", "Prefix": "sources/"}
        return [
            {"Contents": [{"Key": "sources/z"}]},
            {"Contents": [{"Key": "sources/a"}]},
        ]


class FakeS3Client:
    def __init__(self) -> None:
        self.put_arguments: dict[str, Any] | None = None
        self.multipart_parts: list[dict[str, Any]] = []
        self.complete_arguments: dict[str, Any] | None = None
        self.abort_arguments: dict[str, Any] | None = None

    def get_paginator(self, operation: str) -> FakePaginator:
        assert operation == "list_objects_v2"
        return FakePaginator()

    def get_object(self, **arguments: Any) -> dict[str, Any]:
        if arguments["Key"] == "missing":
            raise _client_error("NoSuchKey", "GetObject")
        return {"Body": BytesIO(b"manifest")}

    def head_object(self, **arguments: Any) -> dict[str, Any]:
        if arguments["Key"] == "missing":
            raise _client_error("404", "HeadObject")
        return {
            "ContentLength": 8,
            "ContentType": "text/plain",
            "Metadata": {"sha256": "abc"},
        }

    def put_object(self, **arguments: Any) -> None:
        if arguments["Key"] == "occupied":
            raise _client_error("PreconditionFailed", "PutObject")
        self.put_arguments = arguments

    def create_multipart_upload(self, **arguments: Any) -> dict[str, str]:
        self.put_arguments = arguments
        return {"UploadId": "upload-1"}

    def upload_part(self, **arguments: Any) -> dict[str, str]:
        self.multipart_parts.append(arguments)
        return {"ETag": f"etag-{arguments['PartNumber']}"}

    def complete_multipart_upload(self, **arguments: Any) -> None:
        if arguments["Key"] == "occupied":
            raise _client_error("PreconditionFailed", "CompleteMultipartUpload")
        self.complete_arguments = arguments

    def abort_multipart_upload(self, **arguments: Any) -> None:
        self.abort_arguments = arguments


def test_lists_reads_and_maps_not_found() -> None:
    store = Boto3ObjectStore("bucket", client=FakeS3Client())

    assert store.list_keys("sources/") == ("sources/a", "sources/z")
    assert store.read_bytes("manifest") == b"manifest"
    assert store.read_bytes("missing") is None
    assert store.stat("missing") is None
    assert store.stat("file").metadata == {"sha256": "abc"}  # type: ignore[union-attr]


def test_file_and_manifest_writes_are_conditional(tmp_path: Path) -> None:
    client = FakeS3Client()
    store = Boto3ObjectStore("bucket", client=client)
    path = tmp_path / "data.tsv"
    content = b"content"
    path.write_bytes(content)
    digest = hashlib.sha256(content).hexdigest()

    assert store.put_file_if_absent(
        path,
        "file",
        content_type="text/plain",
        metadata={"sha256": digest},
    )
    assert client.put_arguments is not None
    assert client.put_arguments["IfNoneMatch"] == "*"
    assert client.put_arguments["Metadata"] == {"sha256": digest}
    assert not store.put_file_if_absent(
        path,
        "occupied",
        content_type=None,
        metadata={},
    )
    assert store.put_bytes_if_absent("manifest", b"yaml", content_type="application/yaml")
    assert not store.put_bytes_if_absent(
        "occupied",
        b"yaml",
        content_type="application/yaml",
    )


def test_large_file_uses_conditional_multipart_upload(
    tmp_path: Path, monkeypatch: Any
) -> None:
    monkeypatch.setattr(object_store_module, "MULTIPART_UPLOAD_THRESHOLD_BYTES", 1)
    monkeypatch.setattr(object_store_module, "MINIMUM_MULTIPART_PART_BYTES", 4)
    client = FakeS3Client()
    store = Boto3ObjectStore("bucket", client=client)
    path = tmp_path / "large.parquet"
    content = b"abcdefghijkl"
    path.write_bytes(content)
    digest = hashlib.sha256(content).hexdigest()

    assert store.put_file_if_absent(
        path,
        "large.parquet",
        content_type="application/vnd.apache.parquet",
        metadata={"sha256": digest},
    )

    assert client.put_arguments == {
        "Bucket": "bucket",
        "Key": "large.parquet",
        "Metadata": {"sha256": digest},
        "ContentType": "application/vnd.apache.parquet",
    }
    assert b"".join(part["Body"] for part in client.multipart_parts) == content
    assert client.complete_arguments is not None
    assert client.complete_arguments["IfNoneMatch"] == "*"
    assert client.complete_arguments["MultipartUpload"]["Parts"] == [
        {"PartNumber": 1, "ETag": "etag-1"},
        {"PartNumber": 2, "ETag": "etag-2"},
        {"PartNumber": 3, "ETag": "etag-3"},
    ]
    assert client.abort_arguments is None


def test_multipart_upload_rechecks_client_for_each_s3_operation(
    tmp_path: Path, monkeypatch: Any
) -> None:
    monkeypatch.setattr(object_store_module, "MULTIPART_UPLOAD_THRESHOLD_BYTES", 1)
    monkeypatch.setattr(object_store_module, "MINIMUM_MULTIPART_PART_BYTES", 4)
    client = FakeS3Client()
    store = Boto3ObjectStore("bucket", client=client)
    client_requests = 0

    def refreshing_client() -> FakeS3Client:
        nonlocal client_requests
        client_requests += 1
        return client

    monkeypatch.setattr(store, "_client", refreshing_client)
    path = tmp_path / "large.parquet"
    content = b"abcdefghijkl"
    path.write_bytes(content)

    assert store.put_file_if_absent(
        path,
        "large.parquet",
        content_type=None,
        metadata={"sha256": hashlib.sha256(content).hexdigest()},
    )
    assert client_requests == 5  # create, three parts, complete


def test_losing_multipart_writer_aborts_uploaded_parts(
    tmp_path: Path, monkeypatch: Any
) -> None:
    monkeypatch.setattr(object_store_module, "MULTIPART_UPLOAD_THRESHOLD_BYTES", 1)
    client = FakeS3Client()
    store = Boto3ObjectStore("bucket", client=client)
    path = tmp_path / "large.parquet"
    content = b"content"
    path.write_bytes(content)

    assert not store.put_file_if_absent(
        path,
        "occupied",
        content_type=None,
        metadata={"sha256": hashlib.sha256(content).hexdigest()},
    )
    assert client.abort_arguments == {
        "Bucket": "bucket",
        "Key": "occupied",
        "UploadId": "upload-1",
    }


def test_multipart_checksum_mismatch_aborts_without_completing(
    tmp_path: Path, monkeypatch: Any
) -> None:
    monkeypatch.setattr(object_store_module, "MULTIPART_UPLOAD_THRESHOLD_BYTES", 1)
    client = FakeS3Client()
    store = Boto3ObjectStore("bucket", client=client)
    path = tmp_path / "changed.parquet"
    path.write_bytes(b"changed")

    with pytest.raises(InvalidPublicationError, match="changed while"):
        store.put_file_if_absent(
            path,
            "changed.parquet",
            content_type=None,
            metadata={"sha256": hashlib.sha256(b"original").hexdigest()},
        )

    assert client.complete_arguments is None
    assert client.abort_arguments == {
        "Bucket": "bucket",
        "Key": "changed.parquet",
        "UploadId": "upload-1",
    }
