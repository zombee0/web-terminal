"""Opaque sessions, encrypted provider tokens, and guest authorization grants."""

import base64
import hashlib
import hmac
import json
import secrets
import time

from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


def b64(data):
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def unb64(text):
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def digest(token):
    return hashlib.sha256(token.encode()).hexdigest()


class Crypto:
    def __init__(self, encryption_key, signing_key):
        self.cipher = Fernet(encryption_key.encode())
        self.csrf_key = unb64(encryption_key)
        self.signer = Ed25519PrivateKey.from_private_bytes(unb64(signing_key))

    def encrypt(self, text):
        return self.cipher.encrypt(text.encode()).decode()

    def decrypt(self, text):
        return self.cipher.decrypt(text.encode()).decode()

    def csrf(self, token):
        return hmac.new(self.csrf_key, ("csrf:" + token).encode(), hashlib.sha256).hexdigest()

    def grant(self, box_id, path="/ws", method="GET"):
        data = json.dumps({"box": str(box_id), "method": method, "path": path,
                           "exp": int(time.time())+20, "nonce": secrets.token_urlsafe(24)}, separators=(",", ":")).encode()
        return b64(data) + "." + b64(self.signer.sign(data))

    def guest_config(self, box_id):
        key = self.signer.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
        return {"box_id": str(box_id), "public_key": b64(key), "identity": {"uid": 1000, "gid": 1000}}


def new_keys():
    signer = Ed25519PrivateKey.generate()
    private = signer.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw, serialization.NoEncryption())
    return {"encryption_key": Fernet.generate_key().decode(), "signing_key": b64(private)}
