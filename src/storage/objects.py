"""Storage of job inputs (`inputs/…`) and results (`jobs/{job_id}/…`): a local folder or an S3 bucket."""

import asyncio
import mimetypes
import shutil
from pathlib import Path
from typing import Protocol
from urllib.parse import quote, urlencode

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

from src.core.settings import Settings


def content_type(name: str) -> str:
    return mimetypes.guess_type(name)[0] or "application/octet-stream"


class ObjectStore(Protocol):
    async def ensure_bucket(self) -> None: ...
    async def upload(self, path: Path, key: str) -> None: ...
    async def put(self, data: bytes, key: str, name: str) -> None: ...
    async def download(self, key: str, path: Path) -> None: ...
    async def exists(self, key: str) -> bool: ...
    async def delete_prefix(self, prefix: str) -> None: ...
    def url(self, key: str, download_name: str | None = None) -> str: ...


def make_object_store(settings: Settings) -> ObjectStore:
    return S3ObjectStore(settings) if settings.storage == "s3" else LocalObjectStore(settings)


class LocalObjectStore:
    """
    Objects as files under `storage_dir`, served by the API itself at `/files/{key}`.
    For running on one machine; links are not signed — use S3 when the API is public.
    """

    def __init__(self, settings: Settings) -> None:
        self.root = settings.storage_dir.resolve()
        self._public_url = settings.public_url.rstrip("/")

    def path(self, key: str) -> Path:
        """Local file of a key; raises ValueError for keys that leave the storage folder."""
        path = (self.root / key).resolve()
        if not path.is_relative_to(self.root) or path == self.root:
            raise ValueError(f"bad key: {key}")
        return path

    async def ensure_bucket(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)

    async def upload(self, path: Path, key: str) -> None:
        dest = self.path(key)
        dest.parent.mkdir(parents=True, exist_ok=True)
        await asyncio.to_thread(shutil.copyfile, path, dest)

    async def put(self, data: bytes, key: str, name: str) -> None:
        dest = self.path(key)
        dest.parent.mkdir(parents=True, exist_ok=True)
        await asyncio.to_thread(dest.write_bytes, data)

    async def download(self, key: str, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        await asyncio.to_thread(shutil.copyfile, self.path(key), path)

    async def exists(self, key: str) -> bool:
        try:
            return self.path(key).is_file()
        except ValueError:
            return False

    async def delete_prefix(self, prefix: str) -> None:
        target = self.path(prefix.rstrip("/"))
        await asyncio.to_thread(shutil.rmtree, target, True)

    def url(self, key: str, download_name: str | None = None) -> str:
        query = f"?{urlencode({'download': download_name})}" if download_name else ""
        return f"{self._public_url}/files/{quote(key)}{query}"


class S3ObjectStore:
    """
    A bucket over the S3 API (Cloudflare R2 in production, SeaweedFS from docker-compose locally).

    Files never pass through the API on the way out: clients get presigned links and
    download straight from the bucket. boto3 is blocking, so calls run in a thread.
    """

    def __init__(self, settings: Settings) -> None:
        self.bucket = settings.s3_bucket
        self._url_ttl = settings.s3_url_ttl
        self._client = boto3.client(
            "s3",
            endpoint_url=settings.s3_endpoint_url,
            region_name=settings.s3_region,
            aws_access_key_id=settings.s3_access_key or None,
            aws_secret_access_key=settings.s3_secret_key or None,
            config=Config(
                signature_version="s3v4",
                s3={"addressing_style": "path"},
                retries={"max_attempts": 5, "mode": "standard"},
                request_checksum_calculation="when_required",
                response_checksum_validation="when_required",
            ),
        )

    async def upload(self, path: Path, key: str) -> None:
        """Upload a local file (multipart for big ones)."""
        await asyncio.to_thread(
            self._client.upload_file, str(path), self.bucket, key, ExtraArgs={"ContentType": content_type(path.name)}
        )

    async def put(self, data: bytes, key: str, name: str) -> None:
        await asyncio.to_thread(
            self._client.put_object, Bucket=self.bucket, Key=key, Body=data, ContentType=content_type(name)
        )

    async def download(self, key: str, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        await asyncio.to_thread(self._client.download_file, self.bucket, key, str(path))

    async def exists(self, key: str) -> bool:
        try:
            await asyncio.to_thread(self._client.head_object, Bucket=self.bucket, Key=key)
            return True
        except ClientError as e:
            if e.response.get("Error", {}).get("Code") in ("404", "NoSuchKey", "NotFound"):
                return False
            raise

    async def delete_prefix(self, prefix: str) -> None:
        """Delete every object under `prefix`."""
        def _delete() -> None:
            paginator = self._client.get_paginator("list_objects_v2")
            for page in paginator.paginate(Bucket=self.bucket, Prefix=prefix):
                keys = [{"Key": obj["Key"]} for obj in page.get("Contents", [])]
                if keys:
                    self._client.delete_objects(Bucket=self.bucket, Delete={"Objects": keys, "Quiet": True})

        await asyncio.to_thread(_delete)

    def url(self, key: str, download_name: str | None = None) -> str:
        """Presigned GET link; with `download_name` the browser saves the file instead of playing it."""
        params = {"Bucket": self.bucket, "Key": key}
        if download_name:
            params["ResponseContentDisposition"] = f"attachment; filename*=UTF-8''{quote(download_name)}"
        return self._client.generate_presigned_url("get_object", Params=params, ExpiresIn=self._url_ttl)

    async def ensure_bucket(self) -> None:
        """Create the bucket if it is missing and ensure CORS allows browser video playback."""
        try:
            await asyncio.to_thread(self._client.head_bucket, Bucket=self.bucket)
        except ClientError:
            await asyncio.to_thread(self._client.create_bucket, Bucket=self.bucket)
        try:
            await asyncio.to_thread(
                self._client.put_bucket_cors,
                Bucket=self.bucket,
                CORSConfiguration={
                    "CORSRules": [
                        {
                            "AllowedOrigins": ["*"],
                            "AllowedMethods": ["GET", "HEAD"],
                            "AllowedHeaders": ["Range", "Content-Type", "*"],
                            "ExposeHeaders": ["Content-Length", "Content-Range", "Accept-Ranges", "ETag"],
                            "MaxAgeSeconds": 3600,
                        }
                    ]
                },
            )
        except ClientError:
            pass
