import hashlib
import hmac
import json
import secrets


TOKEN_BYTES = 32


def generate_token():
    return secrets.token_urlsafe(TOKEN_BYTES)


def hash_token(token):
    if not isinstance(token, str) or not token:
        return ""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def verify_token(token, expected_hash):
    actual_hash = hash_token(token)
    return bool(actual_hash and expected_hash and hmac.compare_digest(actual_hash, expected_hash))


def canonical_json_hash(payload):
    serialized = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()
