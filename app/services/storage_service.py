import uuid
from pathlib import Path

from fastapi import UploadFile

from app.core.config import settings
from app.core.crypto import decrypt_json, encrypt_json


def _ensure_dirs() -> None:
    settings.upload_dir.mkdir(parents=True, exist_ok=True)
    settings.encrypted_dir.mkdir(parents=True, exist_ok=True)


async def save_upload(file: UploadFile) -> tuple[str, Path]:
    _ensure_dirs()

    extension = Path(file.filename or "").suffix or ".mp4"
    video_id = str(uuid.uuid4())
    target = settings.upload_dir / f"{video_id}{extension}"

    content = await file.read()
    target.write_bytes(content)
    return video_id, target


def store_encrypted_analysis(video_id: str, analysis: dict) -> Path:
    _ensure_dirs()
    target = settings.encrypted_dir / f"{video_id}.bin"
    target.write_bytes(encrypt_json(analysis))
    return target


def read_encrypted_analysis(video_id: str) -> dict:
    target = settings.encrypted_dir / f"{video_id}.bin"
    return decrypt_json(target.read_bytes())
