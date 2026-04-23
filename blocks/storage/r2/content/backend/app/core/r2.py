"""Cloudflare R2 client — S3-compatible.

Presign, put, get, delete. We use boto3 against the R2 S3-compatible endpoint
rather than Cloudflare's native JS SDK to keep the Python side portable to any
S3-flavoured provider (MinIO, AWS, Backblaze B2 with a URL swap).
"""

from __future__ import annotations

import os
from functools import lru_cache
from typing import Any

import boto3
from botocore.client import Config


def _endpoint_url() -> str:
    account_id = os.environ["R2_ACCOUNT_ID"]
    return f"https://{account_id}.r2.cloudflarestorage.com"


@lru_cache(maxsize=1)
def r2_client() -> Any:
    """Shared boto3 S3 client configured for R2. Not thread-pool-safe — boto3's
    low-level client is; if you need multiple threads, call this once per pool.
    """
    return boto3.client(
        "s3",
        endpoint_url=_endpoint_url(),
        aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"],
        config=Config(signature_version="s3v4"),
        region_name="auto",
    )


def presign_put(key: str, *, content_type: str, expires: int = 600) -> str:
    """Presigned URL the client can PUT to directly — avoids proxying bytes
    through our backend. Default expiry 10 minutes matches Cloudflare's
    recommendation for browser-driven uploads.
    """
    bucket = os.environ["R2_BUCKET"]
    return r2_client().generate_presigned_url(  # type: ignore[no-any-return]
        "put_object",
        Params={"Bucket": bucket, "Key": key, "ContentType": content_type},
        ExpiresIn=expires,
        HttpMethod="PUT",
    )


def public_url(key: str) -> str:
    """Return the public URL for an object, assuming the bucket has an attached
    public r2.dev subdomain or custom domain configured via R2_PUBLIC_BASE_URL.
    """
    base = os.environ.get("R2_PUBLIC_BASE_URL", "").rstrip("/")
    if not base:
        raise RuntimeError("R2_PUBLIC_BASE_URL not set — cannot build public URL")
    return f"{base}/{key.lstrip('/')}"
