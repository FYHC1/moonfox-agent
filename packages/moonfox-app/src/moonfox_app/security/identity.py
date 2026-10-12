"""Host-injected credentials. No tokens in URLs, configuration responses or log payloads."""

import hashlib
import hmac
from collections.abc import Mapping
from dataclasses import dataclass


class AuthenticationError(PermissionError):
    pass


class AccessDenied(PermissionError):
    pass


@dataclass(frozen=True, slots=True)
class Principal:
    id: str
    is_admin: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.id, str) or not self.id or len(self.id) > 100:
            raise ValueError("Invalid principal identity")


class TokenAuth:
    """Tokens injected at explicit startup, retained only as SHA-256 digests."""

    def __init__(self, credentials: Mapping[str, Principal]) -> None:
        if not credentials or any(len(token) < 24 for token in credentials):
            raise ValueError("At least one high-entropy token of 24+ characters is required")
        self._credentials = tuple(
            (hashlib.sha256(token.encode()).digest(), principal)
            for token, principal in credentials.items()
        )

    def authenticate(self, token: str) -> Principal:
        if not isinstance(token, str) or len(token) > 4096:
            raise AuthenticationError("Invalid credentials")
        digest = hashlib.sha256(token.encode()).digest()
        matched: Principal | None = None
        for candidate, principal in self._credentials:
            if hmac.compare_digest(candidate, digest):
                matched = principal
        if matched is None:
            raise AuthenticationError("Invalid credentials")
        return matched


def authorize_session(principal: Principal, owner_id: str) -> None:
    if not principal.is_admin and principal.id != owner_id:
        raise AccessDenied("Session access denied")
