"""Canonical encoding and content identities shared by storage and capture."""

import hashlib
import json


def encode(value: dict) -> bytes:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()


def fingerprint(value: dict) -> str:
    return hashlib.sha256(encode(value)).hexdigest()


def blob_id(data: bytes) -> str:
    return hashlib.sha1(f"blob {len(data)}\0".encode() + data).hexdigest()
