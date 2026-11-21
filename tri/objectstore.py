"""Shared object storage for blobs, artifacts and secret values.

``TRI_OBJECT_STORE`` selects it:

- ``file:///shared/dir``: a directory every node mounts (NFS, EFS, a SAN);
- ``s3://bucket/prefix``: Amazon S3 or any S3-compatible service (MinIO, R2,
  GCS interoperability). ``TRI_OBJECT_STORE_ENDPOINT`` (or ``?endpoint=``)
  points at a non-AWS service and switches to path-style requests. Credentials
  come from ``AWS_ACCESS_KEY_ID`` / ``AWS_SECRET_ACCESS_KEY`` /
  ``AWS_SESSION_TOKEN`` and the region from ``AWS_REGION`` (default
  ``us-east-1``). Requests are signed with AWS Signature Version 4.

Objects are immutable once written: blobs are content addressed, artifacts and
secret values are written once under unique names. Each node keeps a local
copy under ``TRI_HOME`` and fetches from here on a miss.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib import request
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, quote, urlparse

OBJECT_STORE_ENV = "TRI_OBJECT_STORE"


class ObjectStoreError(Exception):
    def __init__(self, message: str, *, retryable: bool = True) -> None:
        super().__init__(message)
        self.retryable = retryable


class ObjectStore:
    url: str

    def put(self, key: str, data: bytes) -> None:  # pragma: no cover - interface
        raise NotImplementedError

    def get(self, key: str) -> bytes | None:  # pragma: no cover - interface
        raise NotImplementedError

    def delete(self, key: str) -> None:  # pragma: no cover - interface
        raise NotImplementedError

    def describe(self) -> dict[str, str]:
        return {"kind": type(self).__name__.replace("ObjectStore", "").lower() or "object", "url": self.url}


class FileObjectStore(ObjectStore):
    def __init__(self, root: Path, url: str) -> None:
        self.root = root
        self.url = url
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        path = (self.root / key).resolve()
        if self.root.resolve() not in path.parents:
            raise ObjectStoreError(f"object key escapes the store: {key}", retryable=False)
        return path

    def put(self, key: str, data: bytes) -> None:
        path = self._path(key)
        if path.exists():
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        with open(tmp, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)

    def get(self, key: str) -> bytes | None:
        try:
            return self._path(key).read_bytes()
        except FileNotFoundError:
            return None

    def delete(self, key: str) -> None:
        try:
            self._path(key).unlink()
        except FileNotFoundError:
            pass


def _sign(key: bytes, message: str) -> bytes:
    return hmac.new(key, message.encode(), hashlib.sha256).digest()


class S3ObjectStore(ObjectStore):
    def __init__(self, bucket: str, prefix: str, url: str, *, endpoint: str | None, region: str) -> None:
        self.bucket = bucket
        self.prefix = prefix.strip("/")
        self.url = url
        self.region = region
        self.access_key = os.environ.get("AWS_ACCESS_KEY_ID", "")
        self.secret_key = os.environ.get("AWS_SECRET_ACCESS_KEY", "")
        self.session_token = os.environ.get("AWS_SESSION_TOKEN", "")
        if endpoint:
            parsed = urlparse(endpoint)
            self.scheme = parsed.scheme or "https"
            self.host = parsed.netloc
            self.path_style = True
        else:
            self.scheme = "https"
            self.host = f"{bucket}.s3.{region}.amazonaws.com"
            self.path_style = False
        self.timeout = float(os.environ.get("TRI_OBJECT_STORE_TIMEOUT", "30"))

    def _object_path(self, key: str) -> str:
        full = f"{self.prefix}/{key}" if self.prefix else key
        encoded = quote(full, safe="/-_.~")
        return f"/{self.bucket}/{encoded}" if self.path_style else f"/{encoded}"

    def create_bucket(self) -> None:
        """Create the bucket (setup and tests; production buckets are provisioned)."""
        status, _ = self._request("PUT", "", path=f"/{self.bucket}" if self.path_style else "/")
        if status == 404:
            raise ObjectStoreError(f"could not create bucket {self.bucket}", retryable=False)

    def _request(self, method: str, key: str, body: bytes = b"", *, path: str | None = None) -> tuple[int, bytes]:
        path = path or self._object_path(key)
        now = datetime.now(timezone.utc)
        amz_date = now.strftime("%Y%m%dT%H%M%SZ")
        date = now.strftime("%Y%m%d")
        payload_hash = hashlib.sha256(body).hexdigest()
        headers = {"host": self.host, "x-amz-content-sha256": payload_hash, "x-amz-date": amz_date}
        if self.session_token:
            headers["x-amz-security-token"] = self.session_token
        signed_headers = ";".join(sorted(headers))
        canonical = "\n".join([method, path, "", "".join(f"{name}:{headers[name]}\n" for name in sorted(headers)), signed_headers, payload_hash])
        scope = f"{date}/{self.region}/s3/aws4_request"
        to_sign = "\n".join(["AWS4-HMAC-SHA256", amz_date, scope, hashlib.sha256(canonical.encode()).hexdigest()])
        key_date = _sign(("AWS4" + self.secret_key).encode(), date)
        signing_key = _sign(_sign(_sign(key_date, self.region), "s3"), "aws4_request")
        signature = hmac.new(signing_key, to_sign.encode(), hashlib.sha256).hexdigest()
        headers["authorization"] = f"AWS4-HMAC-SHA256 Credential={self.access_key}/{scope}, SignedHeaders={signed_headers}, Signature={signature}"
        req = request.Request(f"{self.scheme}://{self.host}{path}", data=body if method == "PUT" else None, method=method)
        for name, value in headers.items():
            if name != "host":
                req.add_header(name, value)
        attempts = 3
        for attempt in range(attempts):
            try:
                with request.urlopen(req, timeout=self.timeout) as response:
                    return response.status, response.read()
            except HTTPError as exc:
                if exc.code == 404:
                    return 404, b""
                if exc.code >= 500 and attempt + 1 < attempts:
                    time.sleep(0.2 * (attempt + 1))
                    continue
                raise ObjectStoreError(f"object store {method} {key} failed: HTTP {exc.code}", retryable=exc.code >= 500) from exc
            except (URLError, OSError) as exc:
                if attempt + 1 < attempts:
                    time.sleep(0.2 * (attempt + 1))
                    continue
                raise ObjectStoreError(f"object store {method} {key} failed: {exc}") from exc
        raise ObjectStoreError(f"object store {method} {key} failed")  # pragma: no cover

    def put(self, key: str, data: bytes) -> None:
        self._request("PUT", key, data)

    def get(self, key: str) -> bytes | None:
        status, body = self._request("GET", key)
        return None if status == 404 else body

    def delete(self, key: str) -> None:
        self._request("DELETE", key)

    def describe(self) -> dict[str, str]:
        return {"kind": "s3", "url": self.url, "endpoint": f"{self.scheme}://{self.host}", "region": self.region}


def object_store_from_url(url: str) -> ObjectStore:
    parsed = urlparse(url)
    if parsed.scheme == "file":
        return FileObjectStore(Path(parsed.path if not parsed.netloc else f"//{parsed.netloc}{parsed.path}"), url)
    if parsed.scheme == "s3":
        query = {key: values[-1] for key, values in parse_qs(parsed.query).items()}
        endpoint = query.get("endpoint") or os.environ.get("TRI_OBJECT_STORE_ENDPOINT") or None
        region = query.get("region") or os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION") or "us-east-1"
        clean = parsed._replace(query="").geturl()
        return S3ObjectStore(parsed.netloc, parsed.path, clean, endpoint=endpoint, region=region)
    raise ValueError(f"unsupported object store URL {url!r}: use file:///path or s3://bucket/prefix")


def object_store_from_env() -> ObjectStore | None:
    url = os.environ.get(OBJECT_STORE_ENV, "").strip()
    return object_store_from_url(url) if url else None
