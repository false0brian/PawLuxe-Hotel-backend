import base64
import hashlib
import hmac
import json
from datetime import datetime, timezone
from typing import Any

from app.core.config import settings


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(data: str) -> bytes:
    padding = "=" * ((4 - (len(data) % 4)) % 4)
    return base64.urlsafe_b64decode(data + padding)


def sign_payload(payload: dict[str, Any]) -> str:
    body = json.dumps(payload, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    body_part = _b64url(body)
    sig = hmac.new(settings.stream_signing_key.encode("utf-8"), body_part.encode("ascii"), hashlib.sha256).digest()
    return f"{body_part}.{_b64url(sig)}"


def parse_and_verify(token: str) -> dict[str, Any]:
    try:
        body_part, sig_part = token.split(".", 1)
    except ValueError as exc:
        raise ValueError("Invalid token format") from exc

    expected_sig = hmac.new(
        settings.stream_signing_key.encode("utf-8"), body_part.encode("ascii"), hashlib.sha256
    ).digest()
    actual_sig = _b64url_decode(sig_part)
    if not hmac.compare_digest(expected_sig, actual_sig):
        raise ValueError("Invalid token signature")

    payload = json.loads(_b64url_decode(body_part).decode("utf-8"))
    exp = int(payload.get("exp", 0))
    if exp <= int(datetime.now(timezone.utc).timestamp()):
        raise ValueError("Token expired")
    return payload
