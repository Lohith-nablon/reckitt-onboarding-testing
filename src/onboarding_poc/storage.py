"""Storage: S3 in production, a local folder for development and tests. Same keys either way.

All state is plain files: session state and trace as JSON, uploads and outputs as the files
themselves. Keys are '/'-separated and relative to the bucket prefix (or local root).
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Protocol

from onboarding_poc.config import AppConfig, S3Settings


class Storage(Protocol):
    def read_bytes(self, key: str) -> bytes: ...
    def write_bytes(self, key: str, data: bytes, content_type: str | None = None) -> None: ...
    def exists(self, key: str) -> bool: ...
    def list(self, prefix: str) -> list[str]: ...
    def delete(self, key: str) -> None: ...
    def uri(self, key: str) -> str: ...


def _clean(o: Any) -> Any:
    """NaN / inf are not JSON: store them as null so every document is valid for the API."""
    if isinstance(o, float) and (o != o or o in (float("inf"), float("-inf"))):
        return None
    if isinstance(o, dict):
        return {k: _clean(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_clean(v) for v in o]
    return o


class StorageMixin:
    def read_json(self, key: str, default: Any = None) -> Any:
        if not self.exists(key):
            return default
        return json.loads(self.read_bytes(key).decode("utf-8"))

    def write_json(self, key: str, obj: Any) -> None:
        self.write_bytes(key, json.dumps(_clean(obj), indent=2, default=str, ensure_ascii=False, allow_nan=False).encode("utf-8"),
                         "application/json")

    def read_text(self, key: str) -> str:
        return self.read_bytes(key).decode("utf-8")

    def write_text(self, key: str, text: str, content_type: str = "text/plain") -> None:
        self.write_bytes(key, text.encode("utf-8"), content_type)


class LocalStorage(StorageMixin):
    def __init__(self, root: str):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _p(self, key: str) -> Path:
        p = (self.root / key).resolve()
        if self.root.resolve() not in p.parents and p != self.root.resolve():
            raise ValueError(f"key escapes storage root: {key}")
        return p

    def read_bytes(self, key: str) -> bytes:
        return self._p(key).read_bytes()

    def write_bytes(self, key: str, data: bytes, content_type: str | None = None) -> None:
        p = self._p(key)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(p.suffix + ".tmp")
        tmp.write_bytes(data)
        os.replace(tmp, p)

    def exists(self, key: str) -> bool:
        return self._p(key).is_file()

    def list(self, prefix: str) -> list[str]:
        base = self._p(prefix) if prefix else self.root
        if base.is_file():
            return [prefix]
        if not base.is_dir():
            return []
        return sorted(str(p.relative_to(self.root)).replace(os.sep, "/") for p in base.rglob("*") if p.is_file()
                      and not p.name.endswith(".tmp"))

    def delete(self, key: str) -> None:
        p = self._p(key)
        if p.is_file():
            p.unlink()

    def uri(self, key: str) -> str:
        return str(self._p(key))


def s3_client(s3: S3Settings, access_key: str | None = None, secret_key: str | None = None,
              session_token: str | None = None, region: str | None = None):
    import boto3
    kwargs: dict[str, Any] = {"region_name": region or s3.region}
    if s3.endpoint_url:
        kwargs["endpoint_url"] = s3.endpoint_url
    ak = access_key or os.environ.get(s3.access_key_env)
    sk = secret_key or os.environ.get(s3.secret_key_env)
    if ak and sk:
        kwargs.update(aws_access_key_id=ak, aws_secret_access_key=sk,
                      aws_session_token=session_token or os.environ.get(s3.session_token_env) or None)
        return boto3.client("s3", **kwargs)
    session = boto3.Session(profile_name=s3.profile) if s3.profile else boto3.Session()
    return session.client("s3", **kwargs)


class S3Storage(StorageMixin):
    def __init__(self, s3: S3Settings, client=None):
        if not s3.bucket:
            raise ValueError("storage.backend is s3 but storage.s3.bucket is empty")
        self.bucket, self.prefix = s3.bucket, s3.prefix.strip("/")
        self.client = client or s3_client(s3)

    def _k(self, key: str) -> str:
        return f"{self.prefix}/{key}" if self.prefix else key

    def read_bytes(self, key: str) -> bytes:
        return self.client.get_object(Bucket=self.bucket, Key=self._k(key))["Body"].read()

    def write_bytes(self, key: str, data: bytes, content_type: str | None = None) -> None:
        extra = {"ContentType": content_type} if content_type else {}
        self.client.put_object(Bucket=self.bucket, Key=self._k(key), Body=data, **extra)

    def exists(self, key: str) -> bool:
        from botocore.exceptions import ClientError
        try:
            self.client.head_object(Bucket=self.bucket, Key=self._k(key))
            return True
        except ClientError:
            return False

    def list(self, prefix: str) -> list[str]:
        out, token = [], None
        full = self._k(prefix)
        strip = len(self.prefix) + 1 if self.prefix else 0
        while True:
            kw = {"Bucket": self.bucket, "Prefix": full}
            if token:
                kw["ContinuationToken"] = token
            r = self.client.list_objects_v2(**kw)
            out += [o["Key"][strip:] for o in r.get("Contents", [])]
            if not r.get("IsTruncated"):
                return sorted(out)
            token = r["NextContinuationToken"]

    def delete(self, key: str) -> None:
        self.client.delete_object(Bucket=self.bucket, Key=self._k(key))

    def uri(self, key: str) -> str:
        return f"s3://{self.bucket}/{self._k(key)}"


def build_storage(cfg: AppConfig):
    if cfg.storage.backend == "s3":
        return S3Storage(cfg.storage.s3)
    return LocalStorage(cfg.storage.local_root)


def parse_s3_uri(uri: str) -> tuple[str, str]:
    if not uri.startswith("s3://"):
        raise ValueError(f"not an s3:// path: {uri}")
    bucket, _, key = uri[5:].partition("/")
    return bucket, key
