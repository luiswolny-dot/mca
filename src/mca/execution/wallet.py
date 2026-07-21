"""Wallet manager: encrypted key custody.

Design decision — scrypt + AES-256-GCM, not the Solana CLI's plaintext-array
------------------------------------------------------------------------------
``solana-keygen``'s default keypair.json is a plaintext JSON array of the raw
secret bytes. That is fine for a throwaway devnet key and unacceptable for
anything holding real funds. This module derives a key from a passphrase with
scrypt (n=2^18, r=8, p=1 — the parameters already named in
``config.example.yaml``'s ``security:`` section) and encrypts the secret with
AES-256-GCM, which is authenticated: a wrong passphrase or a single flipped
byte in the file raises :class:`InvalidPassphrase` instead of silently
returning garbage that gets fed into a signature.

Design decision — the public key is derivable without solders
-----------------------------------------------------------------
``solders`` is an optional dependency (the ``chains`` extra) so that the API,
scorer, and backtester containers — which must never load a key at all — do
not need it on their image. But *identifying* which wallet a keystore file
belongs to is useful everywhere (dashboards, logs, the sweep-threshold check),
so the public-key derivation here uses only ``cryptography``'s Ed25519 support
plus a small dependency-free base58 encoder, and works in every container.
Only :meth:`WalletManager.unlock`, which hands back a signable ``solders``
``Keypair``, requires the optional dependency — and it fails with a clear
message rather than an import traceback if that dependency is absent.

Design decision — this cannot promise to erase the key from process memory
------------------------------------------------------------------------------
CPython strings and ``bytes`` are immutable; there is no reliable way to zero a
secret's backing memory from pure Python once it has been created, and pretending
otherwise would be a false promise. What this module *can* guarantee: the secret
is decrypted once per process into a local variable, handed directly to the
signer, and never logged, returned from an API, or written back to disk. That is
a materially weaker guarantee than what a hardware wallet gives you, and is the
reason the hot wallet has a spending cap in the first place (see
``execution/base.py``).
"""

from __future__ import annotations

import base64
import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

try:
    from solders.keypair import Keypair  # type: ignore
except ImportError:
    Keypair = None  # type: ignore[assignment,misc]

# scrypt parameters, matched to config.example.yaml's security.keystore_kdf comment.
SCRYPT_N = 2 ** 18
SCRYPT_R = 8
SCRYPT_P = 1
KEY_LEN = 32
_AAD = b"mca-keystore-v1"
_B58_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


class InvalidPassphrase(Exception):
    """Wrong passphrase, or the keystore file was corrupted/tampered with.

    AES-GCM cannot distinguish the two cases — that is the point of an
    authenticated cipher: a corrupted ciphertext and a wrong key both fail
    exactly the same way, with no partial "almost decrypted" result to leak
    information to an attacker.
    """


def b58encode(raw: bytes) -> str:
    """Minimal Bitcoin/IPFS-alphabet base58 encoder — what Solana addresses use.

    Written dependency-free (no ``base58`` package needed) so that public-key
    derivation works in every container, including ones that never install the
    ``chains`` extra.
    """
    n = int.from_bytes(raw, "big")
    out = bytearray()
    while n > 0:
        n, rem = divmod(n, 58)
        out.append(ord(_B58_ALPHABET[rem]))
    out.reverse()
    n_leading_zero_bytes = len(raw) - len(raw.lstrip(b"\x00"))
    return (_B58_ALPHABET[0] * n_leading_zero_bytes) + out.decode("ascii")


def _pubkey_from_secret(secret_key: bytes) -> bytes:
    """32 raw pubkey bytes, from either a 32-byte seed or a 64-byte
    seed+pubkey secret (the format Solana CLI keypair files use)."""
    if len(secret_key) == 64:
        return secret_key[32:]
    if len(secret_key) == 32:
        priv = Ed25519PrivateKey.from_private_bytes(secret_key)
        return priv.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    raise ValueError(f"secret key must be 32 (seed) or 64 (seed+pubkey) bytes, got {len(secret_key)}")


@dataclass(slots=True, frozen=True)
class EncryptedKeystore:
    version: int
    kdf: str
    n: int
    r: int
    p: int
    salt: str          # base64
    nonce: str          # base64
    ciphertext: str      # base64, AES-GCM tag included
    pubkey: str          # base58, cleartext — identification only, never secret

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2, sort_keys=True)

    @classmethod
    def from_json(cls, text: str) -> "EncryptedKeystore":
        return cls(**json.loads(text))

    def save(self, path: str | Path) -> None:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(self.to_json(), encoding="utf-8")
        os.chmod(p, 0o600)

    @classmethod
    def load(cls, path: str | Path) -> "EncryptedKeystore":
        return cls.from_json(Path(path).read_text(encoding="utf-8"))


class WalletManager:
    """Creates and unlocks :class:`EncryptedKeystore` files. Stateless — holds
    no key material between calls; the caller decides how long a decrypted
    keypair stays in memory."""

    @staticmethod
    def _derive_key(passphrase: str, salt: bytes, n: int = SCRYPT_N,
                    r: int = SCRYPT_R, p: int = SCRYPT_P) -> bytes:
        if not passphrase:
            raise ValueError("empty passphrase")
        kdf = Scrypt(salt=salt, length=KEY_LEN, n=n, r=r, p=p)
        return kdf.derive(passphrase.encode("utf-8"))

    @classmethod
    def create(cls, secret_key: bytes, passphrase: str) -> EncryptedKeystore:
        """Encrypt ``secret_key`` (32-byte seed or 64-byte Solana secret) under
        ``passphrase``. The caller is responsible for the passphrase's strength
        and for how it is supplied (env var, prompt, secrets manager) — this
        function only ever sees it as an in-memory argument."""
        pubkey_bytes = _pubkey_from_secret(secret_key)  # validates length as a side effect
        salt = os.urandom(16)
        nonce = os.urandom(12)
        key = cls._derive_key(passphrase, salt)
        ciphertext = AESGCM(key).encrypt(nonce, secret_key, _AAD)
        return EncryptedKeystore(
            version=1, kdf="scrypt", n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P,
            salt=base64.b64encode(salt).decode(),
            nonce=base64.b64encode(nonce).decode(),
            ciphertext=base64.b64encode(ciphertext).decode(),
            pubkey=b58encode(pubkey_bytes),
        )

    @classmethod
    def unlock_raw(cls, ks: EncryptedKeystore, passphrase: str) -> bytes:
        """Decrypt and return the raw secret bytes. Prefer :meth:`unlock` if
        you need a signer — this exists mainly so the crypto round trip can be
        tested without the optional ``solders`` dependency."""
        if ks.kdf != "scrypt":
            raise ValueError(f"unsupported KDF {ks.kdf!r}")
        salt = base64.b64decode(ks.salt)
        nonce = base64.b64decode(ks.nonce)
        ciphertext = base64.b64decode(ks.ciphertext)
        key = cls._derive_key(passphrase, salt, ks.n, ks.r, ks.p)
        try:
            secret = AESGCM(key).decrypt(nonce, ciphertext, _AAD)
        except InvalidTag as exc:
            raise InvalidPassphrase(
                "wrong passphrase or corrupted keystore file") from exc
        if b58encode(_pubkey_from_secret(secret)) != ks.pubkey:
            # Cannot happen if the AEAD tag verified — kept as a defence-in-depth
            # assertion in case a future format change decouples the two checks.
            raise InvalidPassphrase("decrypted key does not match the stored pubkey hint")
        return secret

    @classmethod
    def unlock(cls, ks: EncryptedKeystore, passphrase: str):
        """Decrypt and return a signable ``solders.keypair.Keypair``."""
        if Keypair is None:
            raise RuntimeError(
                "solders is not installed: pip install 'mca[chains]' "
                "(this container is not supposed to hold live keys unless it is "
                "the executor — see execution/base.py)")
        secret = cls.unlock_raw(ks, passphrase)
        return Keypair.from_bytes(secret) if len(secret) == 64 else Keypair.from_seed(secret)


def hot_wallet_sweep_amount_usd(balance_usd, cfg) -> "object":
    """How much of the hot wallet's balance should move to cold storage right now.

    Pure function over :class:`~mca.execution.base.ExecutionConfig`'s
    ``hot_wallet_max_usd`` / ``sweep_threshold_usd``, reused by both the
    scheduled sweep job and its unit test. Returns ``Decimal(0)`` when no sweep
    is due — a hot wallet under the cap is the normal, expected state, not an
    edge case to special-case at every call site.
    """
    from decimal import Decimal
    if balance_usd <= cfg.sweep_threshold_usd:
        return Decimal(0)
    keep = cfg.hot_wallet_max_usd
    return max(Decimal(0), balance_usd - keep)
