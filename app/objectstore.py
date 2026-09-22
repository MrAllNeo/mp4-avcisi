"""Optional S3-compatible offload for finished downloads.

Serving finished files from the application container makes egress the single
largest variable cost: every completed job streams its whole output through the
host. Cloudflare R2 charges nothing for egress, so when it is configured the
file is uploaded once and the client is redirected to a short-lived presigned
URL instead.

Everything here is opt-in. With no configuration the caller falls back to
serving the local file exactly as before.
"""

from __future__ import annotations

import os
import threading

try:  # boto3 is only needed when the offload is switched on.
    import boto3
    from botocore.config import Config
    from botocore.exceptions import BotoCoreError, ClientError
except ImportError:  # pragma: no cover - exercised by deployments without boto3
    boto3 = None
    Config = None
    BotoCoreError = ClientError = Exception

DEFAULT_URL_TTL = 3600
_client_lock = threading.Lock()
_client = None


class ObjectStoreError(RuntimeError):
    pass


def _setting(name: str) -> str:
    return (os.environ.get(name) or "").strip()


def endpoint() -> str:
    return _setting("MP4_R2_ENDPOINT")


def bucket() -> str:
    return _setting("MP4_R2_BUCKET")


def url_ttl() -> int:
    try:
        return max(60, min(int(os.environ.get("MP4_R2_URL_TTL", DEFAULT_URL_TTL)), 604800))
    except (TypeError, ValueError):
        return DEFAULT_URL_TTL


def is_configured() -> bool:
    return bool(
        boto3
        and endpoint()
        and bucket()
        and _setting("MP4_R2_ACCESS_KEY_ID")
        and _setting("MP4_R2_SECRET_ACCESS_KEY")
    )


def reset_client() -> None:
    """Drop the cached client so configuration changes take effect."""
    global _client
    with _client_lock:
        _client = None


def _get_client():
    global _client
    with _client_lock:
        if _client is None:
            if not is_configured():
                raise ObjectStoreError("Nesne deposu yapılandırılmamış.")
            _client = boto3.client(
                "s3",
                endpoint_url=endpoint(),
                aws_access_key_id=_setting("MP4_R2_ACCESS_KEY_ID"),
                aws_secret_access_key=_setting("MP4_R2_SECRET_ACCESS_KEY"),
                # R2 ignores the region but the signer requires one.
                region_name=_setting("MP4_R2_REGION") or "auto",
                config=Config(signature_version="s3v4", retries={"max_attempts": 3}),
            )
        return _client


def object_key(job_id: str) -> str:
    return f"downloads/{job_id}/video.mp4"


def upload(path, job_id: str) -> str:
    """Upload a finished file and return its object key."""
    key = object_key(job_id)
    try:
        _get_client().upload_file(
            str(path), bucket(), key, ExtraArgs={"ContentType": "video/mp4"}
        )
    except (BotoCoreError, ClientError, OSError) as exc:
        raise ObjectStoreError(f"Nesne deposuna yükleme başarısız: {exc}") from exc
    return key


def presigned_url(job_id: str, filename: str) -> str:
    key = object_key(job_id)
    disposition = f'attachment; filename="{filename}"'
    try:
        return _get_client().generate_presigned_url(
            "get_object",
            Params={
                "Bucket": bucket(),
                "Key": key,
                "ResponseContentDisposition": disposition,
                "ResponseContentType": "video/mp4",
            },
            ExpiresIn=url_ttl(),
        )
    except (BotoCoreError, ClientError) as exc:
        raise ObjectStoreError(f"İmzalı adres üretilemedi: {exc}") from exc


def delete(job_id: str) -> None:
    """Remove a stored object. Never raises: cleanup must not break a purge."""
    if not is_configured():
        return
    try:
        _get_client().delete_object(Bucket=bucket(), Key=object_key(job_id))
    except (ObjectStoreError, BotoCoreError, ClientError):
        pass
