"""Public download verification (§5.4, §6).

When a public link requires email verification, an anonymous visitor must prove
control of an email address before downloading — without creating an account.
The challenge is bound to the link's current token (a snapshot); if the owner
rotates the token, outstanding challenges become stale and are rejected.
"""
from __future__ import annotations

import hashlib

from django.db import models
from django.utils import timezone


class PublicDownloadVerification(models.Model):
    stored_file = models.ForeignKey(
        "files.StoredFile", on_delete=models.CASCADE, related_name="public_verifications"
    )
    email = models.EmailField()
    code_hash = models.CharField(max_length=64)
    # Token at issue time — detects a stale link after the owner rotates it.
    token_snapshot = models.CharField(max_length=64)
    attempts = models.PositiveSmallIntegerField(default=0)
    created_at = models.DateTimeField(default=timezone.now)
    expires_at = models.DateTimeField(db_index=True)

    class Meta:
        db_table = "public_download_verification"
        indexes = [models.Index(fields=["stored_file", "email"])]

    def __str__(self):
        return f"verify {self.email} for {self.stored_file_id}"

    @staticmethod
    def hash_code(code: str) -> str:
        return hashlib.sha256(code.encode()).hexdigest()

    @property
    def is_expired(self) -> bool:
        return timezone.now() >= self.expires_at

    def matches(self, code: str, current_token: str) -> bool:
        """Code correct, not expired, and the link token still matches."""
        return (
            not self.is_expired
            and self.token_snapshot == current_token
            and self.code_hash == self.hash_code(code)
        )
