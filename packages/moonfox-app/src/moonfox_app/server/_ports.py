"""Structural service ports: the transport never imports the concrete hub or runtime."""

from typing import Any, Protocol

from moonfox_app.config import HostSettings, SessionSettings
from moonfox_app.security.identity import Principal


class Subscription(Protocol):
    async def next(self) -> dict[str, Any]: ...

    def close(self) -> None: ...


class ServerHub(Protocol):
    async def start(self) -> None: ...

    async def aclose(self) -> None: ...

    async def list_sessions(self, principal: Principal) -> list[dict[str, Any]]: ...

    async def create_session(
        self, principal: Principal, *, title: str, request_id: str
    ) -> dict[str, Any]: ...

    async def snapshot(self, principal: Principal, session_id: str) -> dict[str, Any]: ...

    async def submit_input(
        self,
        principal: Principal,
        session_id: str,
        *,
        text: str,
        request_id: str,
        idempotency_key: str,
    ) -> dict[str, Any]: ...

    async def control(
        self,
        principal: Principal,
        session_id: str,
        *,
        action: str,
        request_id: str,
        input_id: str | None = None,
    ) -> dict[str, Any]: ...

    async def update_session_config(
        self,
        principal: Principal,
        session_id: str,
        *,
        settings: SessionSettings,
        expected_revision: int,
    ) -> dict[str, Any]: ...

    async def config(self, principal: Principal) -> dict[str, Any]: ...

    async def update_config(
        self, principal: Principal, *, settings: HostSettings, expected_revision: int
    ) -> dict[str, Any]: ...

    async def resolve_approval(
        self, principal: Principal, session_id: str, approval_id: str, *, decision: str
    ) -> dict[str, Any]: ...

    def subscribe(
        self, principal: Principal, session_id: str, *, epoch: str, after_seq: int
    ) -> Subscription:
        """Authorize and register atomically, raising ResyncRequired for an invalid cursor."""
        ...
