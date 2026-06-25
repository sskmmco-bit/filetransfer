"""Low-level MinIO (S3) object operations (§3, §5.5).

django-storages handles simple saves, but the resumable chunked path needs
direct control over S3 multipart uploads, server-side copy, and presigned URLs.
This module is a thin boto3 wrapper around the MinIO bucket configured in
settings. All keys are relative to MINIO_BUCKET.
"""
from __future__ import annotations

import boto3
from botocore.client import Config
from django.conf import settings

_client = None
_presign_client = None


def _build_client(endpoint_url):
    return boto3.client(
        "s3",
        endpoint_url=endpoint_url,
        aws_access_key_id=settings.MINIO_ACCESS_KEY,
        aws_secret_access_key=settings.MINIO_SECRET_KEY,
        region_name=getattr(settings, "MINIO_REGION", "us-east-1"),
        use_ssl=settings.MINIO_USE_SSL,
        config=Config(signature_version="s3v4", s3={"addressing_style": "path"}),
    )


def get_client():
    """Cached boto3 S3 client for server-side ops (loopback endpoint)."""
    global _client
    if _client is None:
        _client = _build_client(settings.MINIO_ENDPOINT_URL)
    return _client


def get_presign_client():
    """Client whose endpoint is browser-reachable — used only to sign URLs.

    The signature binds to this endpoint's host, so the URL must be served from
    the same host the browser hits (settings.MINIO_PUBLIC_ENDPOINT_URL).
    """
    global _presign_client
    public = getattr(settings, "MINIO_PUBLIC_ENDPOINT_URL", "") or settings.MINIO_ENDPOINT_URL
    if public == settings.MINIO_ENDPOINT_URL:
        return get_client()
    if _presign_client is None:
        _presign_client = _build_client(public)
    return _presign_client


def bucket() -> str:
    return settings.MINIO_BUCKET


# ----- direct (single-shot) transfer -------------------------------------
def put_object(key: str, fileobj, content_type: str = "application/octet-stream") -> None:
    get_client().put_object(Bucket=bucket(), Key=key, Body=fileobj, ContentType=content_type)


# ----- multipart (chunked) transfer --------------------------------------
def create_multipart(key: str, content_type: str = "application/octet-stream") -> str:
    resp = get_client().create_multipart_upload(
        Bucket=bucket(), Key=key, ContentType=content_type
    )
    return resp["UploadId"]


def upload_part(key: str, upload_id: str, part_number: int, data: bytes) -> str:
    """Upload one part and return its ETag (needed to complete the upload)."""
    resp = get_client().upload_part(
        Bucket=bucket(), Key=key, UploadId=upload_id, PartNumber=part_number, Body=data
    )
    return resp["ETag"]


def complete_multipart(key: str, upload_id: str, parts: list[dict]) -> None:
    get_client().complete_multipart_upload(
        Bucket=bucket(), Key=key, UploadId=upload_id, MultipartUpload={"Parts": parts}
    )


def abort_multipart(key: str, upload_id: str) -> None:
    try:
        get_client().abort_multipart_upload(Bucket=bucket(), Key=key, UploadId=upload_id)
    except Exception:  # noqa: BLE001 — best-effort cleanup
        pass


# ----- object lifecycle --------------------------------------------------
def copy_object(src_key: str, dst_key: str) -> None:
    get_client().copy_object(
        Bucket=bucket(), Key=dst_key, CopySource={"Bucket": bucket(), "Key": src_key}
    )


def delete_object(key: str) -> None:
    if not key:
        return
    try:
        get_client().delete_object(Bucket=bucket(), Key=key)
    except Exception:  # noqa: BLE001 — best-effort cleanup
        pass


def head_object(key: str) -> dict:
    return get_client().head_object(Bucket=bucket(), Key=key)


def object_exists(key: str) -> bool:
    """True if an object is present at `key` (a HEAD that doesn't 404)."""
    if not key:
        return False
    try:
        get_client().head_object(Bucket=bucket(), Key=key)
        return True
    except Exception:  # noqa: BLE001 — any error (incl. 404) means "not usable"
        return False


def get_object_body(key: str):
    """Return a streaming body for reading an object (e.g. to hash it)."""
    return get_client().get_object(Bucket=bucket(), Key=key)["Body"]


def presigned_get_url(key: str, *, download_name: str, expires: int = 3600,
                      inline: bool = False, content_type: str = "") -> str:
    """Short-lived presigned URL (§5.4).

    inline=True makes the browser render the object in-page (preview) instead of
    downloading it. Signed with the browser-reachable presign client.
    """
    disposition = "inline" if inline else "attachment"
    params = {
        "Bucket": bucket(),
        "Key": key,
        "ResponseContentDisposition": f'{disposition}; filename="{download_name}"',
    }
    if content_type:
        params["ResponseContentType"] = content_type
    return get_presign_client().generate_presigned_url(
        "get_object", Params=params, ExpiresIn=expires
    )
