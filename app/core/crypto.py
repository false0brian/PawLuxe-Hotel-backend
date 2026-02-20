import base64
import hashlib
import json

from cryptography.fernet import Fernet

from app.core.config import settings


def _derive_fernet_key(source: str) -> bytes:
    digest = hashlib.sha256(source.encode("utf-8")).digest()
    return base64.urlsafe_b64encode(digest)


def _get_fernet() -> Fernet:
    # ENCRYPTION_KEY can be either a 32-byte URL-safe base64 key or any passphrase.
    configured = settings.encryption_key.strip()
    if configured:
        try:
            return Fernet(configured.encode("utf-8"))
        except Exception:
            return Fernet(_derive_fernet_key(configured))

    return Fernet(_derive_fernet_key(settings.api_key))


def encrypt_json(payload: dict) -> bytes:
    raw = json.dumps(payload, ensure_ascii=True).encode("utf-8")
    return _get_fernet().encrypt(raw)


def decrypt_json(token: bytes) -> dict:
    decrypted = _get_fernet().decrypt(token)
    return json.loads(decrypted.decode("utf-8"))
