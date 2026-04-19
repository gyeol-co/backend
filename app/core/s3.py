"""S3 client wrapper — AWS S3 (bucket `gyeol`, ap-southeast-2) via aioboto3.

Public reads go through a URL composed from `S3_PUBLIC_BASE_URL + '/' + key`
(no SDK round-trip). SDK is used for presigning private URLs, existence
checks, and future upload flows. `get_s3()` returns a process-wide singleton.
"""

from __future__ import annotations

import aioboto3

from app.core.config import get_settings


class S3Client:
    """Thin wrapper around an `aioboto3.Session` for the gyeol bucket.

    - `public_url(key)` is pure string composition, no network.
    - `presigned_url(key)` / `head_object(key)` open an SDK client per call.
    """

    def __init__(self) -> None:
        settings = get_settings()
        self._session = aioboto3.Session(
            aws_access_key_id=settings.AWS_ACCESS_KEY_ID,
            aws_secret_access_key=settings.AWS_SECRET_ACCESS_KEY,
            region_name=settings.AWS_REGION,
        )
        self._bucket = settings.S3_BUCKET
        self._public_base = settings.S3_PUBLIC_BASE_URL.rstrip("/")

    @property
    def bucket(self) -> str:
        return self._bucket

    def public_url(self, key: str) -> str:
        """Compose a public URL by joining the base URL and object key."""
        return f"{self._public_base}/{key.lstrip('/')}"

    async def presigned_url(self, key: str, *, expires_in: int = 3600) -> str:
        """Generate a presigned GET URL (private-bucket path)."""
        async with self._session.client("s3") as client:
            return await client.generate_presigned_url(
                "get_object",
                Params={"Bucket": self._bucket, "Key": key},
                ExpiresIn=expires_in,
            )

    async def head_object(self, key: str) -> bool:
        """Return True if the object exists, False on 404. Other errors raise."""
        async with self._session.client("s3") as client:
            try:
                await client.head_object(Bucket=self._bucket, Key=key)
                return True
            except client.exceptions.ClientError as e:
                if e.response.get("Error", {}).get("Code") in {"404", "NoSuchKey"}:
                    return False
                raise


_s3: S3Client | None = None


def get_s3() -> S3Client:
    """Return the process-wide S3Client (lazy singleton)."""
    global _s3
    if _s3 is None:
        _s3 = S3Client()
    return _s3
