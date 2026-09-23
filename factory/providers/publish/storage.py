"""Буфер Cloudflare R2 (S3 API, 10 ГБ бесплатно, egress $0) для площадок, которые
забирают видео по ссылке: Instagram, Facebook, Rutube."""
from __future__ import annotations

import os
from pathlib import Path

from ...core.config import Config
from .base import PublishError


def upload_public(cfg: Config, path: Path, key: str) -> str:
    rs = cfg.section("publish.remote_storage")
    if not rs.get("enabled"):
        raise PublishError("remote_storage выключен")
    try:
        import boto3  # noqa: PLC0415
    except ImportError as e:
        raise PublishError("для R2 нужен boto3: pip install -e .[r2]") from e
    s3 = boto3.client("s3", endpoint_url=os.environ.get(rs.get("endpoint_env", "R2_ENDPOINT")),
                      aws_access_key_id=os.environ.get(rs.get("access_key_env", "R2_ACCESS_KEY_ID")),
                      aws_secret_access_key=os.environ.get(rs.get("secret_key_env", "R2_SECRET_ACCESS_KEY")),
                      region_name="auto")
    s3.upload_file(str(path), rs["bucket"], key, ExtraArgs={"ContentType": "video/mp4"})
    base = rs.get("public_base_url", "").rstrip("/")
    if not base:
        raise PublishError("remote_storage.public_base_url не задан")
    return f"{base}/{key}"


def delete_public(cfg: Config, key: str) -> None:
    rs = cfg.section("publish.remote_storage")
    try:
        import boto3  # noqa: PLC0415
        s3 = boto3.client("s3", endpoint_url=os.environ.get(rs.get("endpoint_env", "R2_ENDPOINT")),
                          aws_access_key_id=os.environ.get(rs.get("access_key_env", "R2_ACCESS_KEY_ID")),
                          aws_secret_access_key=os.environ.get(rs.get("secret_key_env", "R2_SECRET_ACCESS_KEY")),
                          region_name="auto")
        s3.delete_object(Bucket=rs["bucket"], Key=key)
    except Exception:  # noqa: BLE001 — уборка буфера не критична (10 ГБ, чистится по возрасту)
        pass
