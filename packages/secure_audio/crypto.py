from base64 import b64decode, b64encode
from binascii import Error as Base64Error
from hashlib import sha256
import json
from pathlib import Path
import os

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
import rfc8785


def digest(data: bytes) -> str:
    return sha256(data).hexdigest()


def canonical(value: dict | list) -> bytes:
    return rfc8785.dumps(value)


class EvidenceError(ValueError):
    pass


def _unique_object(pairs: list[tuple[str, object]]) -> dict:
    result = dict(pairs)
    if len(result) != len(pairs):
        raise EvidenceError("Duplicate JSON member")
    return result


def read_json(data: str | bytes):
    return json.loads(data, object_pairs_hook=_unique_object)


class EvidenceSigner:
    def __init__(self, key: Ed25519PrivateKey):
        self._key = key
        self.public_key = key.public_key().public_bytes_raw()
        self.key_id = digest(self.public_key)

    def sign(self, record_type: str, fields: dict) -> dict:
        payload = {**fields, "schema_version": 1, "record_type": record_type, "key_id": self.key_id}
        signature = self._key.sign(canonical(payload))
        return {"payload": payload, "signature": b64encode(signature).decode("ascii")}


def verify_evidence(envelope: dict, public_key: bytes, record_type: str, media_sha256: str | None = None) -> dict:
    try:
        if not isinstance(envelope, dict) or set(envelope) != {"payload", "signature"}:
            raise EvidenceError("Invalid evidence envelope")
        payload = envelope["payload"]
        if not isinstance(payload, dict) or type(payload.get("schema_version")) is not int:
            raise EvidenceError("Invalid evidence payload")
        if payload["schema_version"] != 1 or payload.get("record_type") != record_type:
            raise EvidenceError("Unexpected evidence type or version")
        if payload.get("key_id") != digest(public_key):
            raise EvidenceError("Evidence key is untrusted")
        signature = b64decode(envelope["signature"], validate=True)
        Ed25519PublicKey.from_public_bytes(public_key).verify(signature, canonical(payload))
    except EvidenceError:
        raise
    except (InvalidSignature, Base64Error, ValueError, TypeError) as exc:
        raise EvidenceError("Invalid evidence signature or encoding") from exc
    if media_sha256 is not None and payload.get("media_sha256") != media_sha256:
        raise EvidenceError("Media digest does not match the signed record")
    return payload


class MediaCipher:
    def __init__(self, key: bytes):
        self._cipher = AESGCM(key)

    def encrypt(self, media: bytes, associated_data: bytes) -> bytes:
        nonce = os.urandom(12)
        return nonce + self._cipher.encrypt(nonce, media, associated_data)

    def decrypt(self, ciphertext: bytes, associated_data: bytes) -> bytes:
        return self._cipher.decrypt(ciphertext[:12], ciphertext[12:], associated_data)


def create_keys(directory: Path) -> None:
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    private_path = directory / "signing.key"
    encryption_path = directory / "media.key"
    public_path = directory / "signing.pub"
    if any(path.exists() for path in (private_path, encryption_path, public_path)):
        raise ValueError("Key files already exist")
    private = Ed25519PrivateKey.generate()
    for path, data in (
        (private_path, private.private_bytes_raw()),
        (encryption_path, AESGCM.generate_key(bit_length=256)),
        (public_path, private.public_key().public_bytes_raw()),
    ):
        with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as stream:
            stream.write(data)


def load_keys(directory: Path) -> tuple[EvidenceSigner, MediaCipher]:
    signer = EvidenceSigner(Ed25519PrivateKey.from_private_bytes((directory / "signing.key").read_bytes()))
    if (directory / "signing.pub").read_bytes() != signer.public_key:
        raise ValueError("Signing public and private keys do not correspond")
    return signer, MediaCipher((directory / "media.key").read_bytes())
