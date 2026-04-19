from __future__ import annotations

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError

_hasher = PasswordHasher(
    time_cost=3,
    memory_cost=65536,
    parallelism=4,
    hash_len=32,
    salt_len=16,
)

# Precomputed hash for timing-equalization when the email row is missing.
# Verified against a fixed incorrect password so the call always fails but spends argon2 time.
_DUMMY_HASH = _hasher.hash("dummy-password-for-timing-equalization")


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    try:
        return _hasher.verify(password_hash, password)
    except VerifyMismatchError:
        return False
    except Exception:
        return False


def dummy_verify() -> None:
    """Run a verify against a known hash to equalize timing on the missing-user path."""
    try:
        _hasher.verify(_DUMMY_HASH, "wrong-password")
    except Exception:
        pass
