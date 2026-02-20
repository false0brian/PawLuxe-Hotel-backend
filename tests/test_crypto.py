from app.core.crypto import decrypt_json, encrypt_json


def test_encrypt_decrypt_roundtrip() -> None:
    payload = {"a": 1, "b": "x"}
    token = encrypt_json(payload)
    decoded = decrypt_json(token)
    assert decoded == payload
