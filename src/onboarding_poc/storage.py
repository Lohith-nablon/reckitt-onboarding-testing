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


class AdlsStorage(StorageMixin):
    """ADLS Gen2 via azure-storage-file-datalake. Keys sit under <container>/<prefix>/."""

    def __init__(self, a, client=None):
        if not a.account:
            raise ValueError("storage.backend is adls but storage.adls.account is empty")
        self.a = a
        self.prefix = a.prefix.strip("/")
        if client is None:
            from azure.storage.filedatalake import DataLakeServiceClient
            url = f"https://{a.account}.dfs.core.windows.net"
            key, sas = os.environ.get(a.account_key_env), os.environ.get(a.sas_token_env)
            if key:
                cred = key
            elif sas:
                cred = sas
            else:
                from azure.identity import DefaultAzureCredential
                cred = DefaultAzureCredential()
            client = DataLakeServiceClient(account_url=url, credential=cred).get_file_system_client(a.container)
        self.fs = client

    def _k(self, key: str) -> str:
        return f"{self.prefix}/{key}" if self.prefix else key

    def read_bytes(self, key: str) -> bytes:
        return self.fs.get_file_client(self._k(key)).download_file().readall()

    def write_bytes(self, key: str, data: bytes, content_type: str | None = None) -> None:
        from azure.storage.filedatalake import ContentSettings
        self.fs.get_file_client(self._k(key)).upload_data(
            data, overwrite=True, content_settings=ContentSettings(content_type=content_type) if content_type else None)

    def exists(self, key: str) -> bool:
        return self.fs.get_file_client(self._k(key)).exists()

    def list(self, prefix: str) -> list[str]:
        strip = len(self.prefix) + 1 if self.prefix else 0
        try:
            return sorted(p.name[strip:] for p in self.fs.get_paths(path=self._k(prefix), recursive=True) if not p.is_directory)
        except Exception as exc:   # path not found
            if "PathNotFound" in str(exc) or "404" in str(exc):
                return []
            raise

    def delete(self, key: str) -> None:
        self.fs.get_file_client(self._k(key)).delete_file()

    def uri(self, key: str) -> str:
        return self.a.abfss(key)


class VolumeStorage(StorageMixin):
    """Unity Catalog volume through the Databricks Files API (databricks-sdk). Inside a Databricks
    App the SDK authenticates as the app's service principal; locally it uses DATABRICKS_HOST/TOKEN."""

    def __init__(self, path: str, client=None):
        if not path.startswith("/Volumes/"):
            raise ValueError("storage.volume.path must look like /Volumes/<catalog>/<schema>/<volume>/<folder>")
        self.root = path.rstrip("/")
        if client is None:
            from databricks.sdk import WorkspaceClient
            client = WorkspaceClient()
        self.w = client

    def _p(self, key: str) -> str:
        return f"{self.root}/{key}".rstrip("/")

    def read_bytes(self, key: str) -> bytes:
        return self.w.files.download(self._p(key)).contents.read()

    def write_bytes(self, key: str, data: bytes, content_type: str | None = None) -> None:
        import io
        self.w.files.upload(self._p(key), io.BytesIO(data), overwrite=True)

    def exists(self, key: str) -> bool:
        try:
            self.w.files.get_metadata(self._p(key))
            return True
        except Exception:
            return False

    def list(self, prefix: str) -> list[str]:
        start = self._p(prefix)
        try:
            self.w.files.get_metadata(start)
            return [prefix]                      # a file
        except Exception:
            pass
        out, todo = [], [start]
        while todo:
            d = todo.pop()
            try:
                entries = list(self.w.files.list_directory_contents(d))
            except Exception:
                continue
            for e in entries:
                if e.is_directory:
                    todo.append(e.path.rstrip("/"))
                else:
                    out.append(e.path[len(self.root) + 1:])
        return sorted(out)

    def delete(self, key: str) -> None:
        self.w.files.delete(self._p(key))

    def uri(self, key: str) -> str:
        return self._p(key)


def build_storage(cfg: AppConfig):
    if cfg.storage.backend == "volume":
        return VolumeStorage(cfg.storage.volume.path)
    if cfg.storage.backend == "adls":
        return AdlsStorage(cfg.storage.adls)
    if cfg.storage.backend == "s3":
        return S3Storage(cfg.storage.s3)
    return LocalStorage(cfg.storage.local_root)


def parse_s3_uri(uri: str) -> tuple[str, str]:
    if not uri.startswith("s3://"):
        raise ValueError(f"not an s3:// path: {uri}")
    bucket, _, key = uri[5:].partition("/")
    return bucket, key
