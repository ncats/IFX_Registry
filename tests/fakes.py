"""Small fakes shared by application and presentation tests."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from pathlib import Path

from ifx_registry.domain.errors import InvalidPublicationError
from ifx_registry.infrastructure.object_store import ObjectMetadata


class FakeObjectStore:
    def __init__(self, bucket: str = "test-registry"):
        self.bucket = bucket
        self.objects: dict[str, bytes] = {}
        self.object_metadata: dict[str, ObjectMetadata] = {}
        self.actions: list[tuple[str, str]] = []

    def list_keys(self, prefix: str) -> tuple[str, ...]:
        return tuple(sorted(key for key in self.objects if key.startswith(prefix)))

    def read_bytes(self, key: str) -> bytes | None:
        return self.objects.get(key)

    def stat(self, key: str) -> ObjectMetadata | None:
        return self.object_metadata.get(key)

    def download_file(self, key: str, destination: Path) -> None:
        payload = self.objects.get(key)
        if payload is None:
            raise FileNotFoundError(key)
        destination.write_bytes(payload)
        self.actions.append(("download", key))

    def put_file_if_absent(
        self,
        path: Path,
        key: str,
        *,
        content_type: str | None,
        metadata: Mapping[str, str],
    ) -> bool:
        if key in self.objects:
            return False
        content = path.read_bytes()
        expected = metadata.get("sha256")
        if expected is not None and hashlib.sha256(content).hexdigest() != expected:
            raise InvalidPublicationError(f"Snapshot file changed while being uploaded: {path}")
        self.objects[key] = content
        self.object_metadata[key] = ObjectMetadata(
            key=key,
            size_bytes=len(content),
            content_type=content_type,
            metadata=dict(metadata),
        )
        self.actions.append(("upload", key))
        return True

    def put_bytes_if_absent(self, key: str, payload: bytes, *, content_type: str) -> bool:
        if key in self.objects:
            return False
        self.objects[key] = payload
        self.object_metadata[key] = ObjectMetadata(
            key=key,
            size_bytes=len(payload),
            content_type=content_type,
            metadata={},
        )
        self.actions.append(("commit", key))
        return True
