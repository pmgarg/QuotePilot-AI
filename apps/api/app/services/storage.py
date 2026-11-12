"""Private file storage. Keys are server-generated; files are never exposed through a public URL."""

from __future__ import annotations

import os
import re
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import BinaryIO, Protocol

from app.core.config import get_settings

_SAFE = re.compile(r"[^A-Za-z0-9._-]+")


def safe_filename(name: str, default: str = "file") -> str:
    base = os.path.basename(name or "").strip()
    base = _SAFE.sub("_", base)[:120].strip("._") or default
    return base


def make_key(organization_id: uuid.UUID, kind: str, filename: str) -> str:
    return f"org/{organization_id}/{kind}/{uuid.uuid4()}/{safe_filename(filename)}"


class Storage(Protocol):
    def put(self, key: str, data: bytes) -> None: ...
    def open(self, key: str) -> BinaryIO: ...
    def exists(self, key: str) -> bool: ...
    def delete(self, key: str) -> None: ...
    def local_path(self, key: str) -> Path | None: ...


class LocalStorage:
    def __init__(self, root: str) -> None:
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        if key.startswith("/") or ".." in key.split("/"):
            raise ValueError("invalid storage key")
        p = (self.root / key).resolve()
        if self.root not in p.parents:
            raise ValueError("invalid storage key")
        return p

    def put(self, key: str, data: bytes) -> None:
        p = self._path(key)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(p.suffix + ".tmp")
        with open(tmp, "wb") as fh:
            fh.write(data)
        os.replace(tmp, p)
        os.chmod(p, 0o600)

    def open(self, key: str) -> BinaryIO:
        return open(self._path(key), "rb")

    def exists(self, key: str) -> bool:
        return self._path(key).exists()

    def delete(self, key: str) -> None:
        p = self._path(key)
        if p.exists():
            p.unlink()

    def local_path(self, key: str) -> Path | None:
        return self._path(key)


class S3Storage:
    """Optional S3-compatible backend (boto3 extra). Bucket must be private; SSE recommended."""

    def __init__(self, bucket: str, endpoint_url: str | None, region: str | None) -> None:
        import boto3

        self.bucket = bucket
        self.client = boto3.client("s3", endpoint_url=endpoint_url, region_name=region)

    def put(self, key: str, data: bytes) -> None:
        self.client.put_object(Bucket=self.bucket, Key=key, Body=data, ServerSideEncryption="AES256")

    def open(self, key: str) -> BinaryIO:
        import io

        body = self.client.get_object(Bucket=self.bucket, Key=key)["Body"].read()
        return io.BytesIO(body)

    def exists(self, key: str) -> bool:
        try:
            self.client.head_object(Bucket=self.bucket, Key=key)
            return True
        except Exception:
            return False

    def delete(self, key: str) -> None:
        self.client.delete_object(Bucket=self.bucket, Key=key)

    def local_path(self, key: str) -> Path | None:
        return None


_storage: Storage | None = None


def get_storage() -> Storage:
    global _storage
    if _storage is None:
        s = get_settings()
        if s.storage_backend == "s3":
            if not s.s3_bucket:
                raise RuntimeError("S3_BUCKET required for storage_backend=s3")
            _storage = S3Storage(s.s3_bucket, s.s3_endpoint_url, s.s3_region)
        else:
            _storage = LocalStorage(s.storage_dir)
    return _storage


def reset_storage() -> None:
    global _storage
    _storage = None


def iter_file(fh: BinaryIO, chunk: int = 64 * 1024) -> Iterator[bytes]:
    try:
        while True:
            data = fh.read(chunk)
            if not data:
                break
            yield data
    finally:
        fh.close()
