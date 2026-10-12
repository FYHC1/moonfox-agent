"""SQLite host facts: routing, durable inbox, approvals, budgets and reply outbox.

All methods are synchronous and called on the host's single event loop. Transactions
never span an await, model request, filesystem transcript write or network side effect.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

from moonfox_ai.api import now_ms
from moonfox_app.config import HostSettings, SessionSettings

from .errors import BudgetExceeded, ConflictError, NotFoundError
from .provider_store import SCHEMA as PROVIDER_SCHEMA
from .provider_store import ProviderStore
from .types import Domain, InputRecord, LogicalBudget, SessionRecord

_SCHEMA = """
CREATE TABLE IF NOT EXISTS config (
 id INTEGER PRIMARY KEY CHECK(id=1), revision INTEGER NOT NULL, data TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions (
 id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, domain TEXT NOT NULL CHECK(domain IN ('agent','qq')),
 scope_id TEXT NOT NULL UNIQUE, route_key TEXT, title TEXT NOT NULL,
 created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL,
 settings TEXT NOT NULL, config_revision INTEGER NOT NULL, request_id TEXT NOT NULL,
 UNIQUE(owner_id,domain,request_id), UNIQUE(domain,route_key)
);
CREATE TABLE IF NOT EXISTS budgets (
 id TEXT PRIMARY KEY, max_requests INTEGER NOT NULL, max_tokens INTEGER NOT NULL,
 max_milliseconds INTEGER NOT NULL, requests_used INTEGER NOT NULL DEFAULT 0,
 tokens_used INTEGER NOT NULL DEFAULT 0, milliseconds_used INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS inputs (
 id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES sessions(id),
 request_id TEXT NOT NULL, idempotency_key TEXT NOT NULL, text TEXT NOT NULL,
 kind TEXT NOT NULL CHECK(kind IN ('prompt','continue','compact')),
 logical_id TEXT NOT NULL REFERENCES budgets(id), status TEXT NOT NULL,
 created_at INTEGER NOT NULL, run_id TEXT, error TEXT, baseline_id TEXT,
 UNIQUE(session_id,idempotency_key), UNIQUE(session_id,request_id)
);
CREATE INDEX IF NOT EXISTS input_queue ON inputs(session_id,status,created_at);
CREATE TABLE IF NOT EXISTS approvals (
 id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES sessions(id), input_id TEXT NOT NULL,
 tool_name TEXT NOT NULL, arguments TEXT NOT NULL,
 status TEXT NOT NULL, expires_at INTEGER NOT NULL,
 decision_by TEXT
);
CREATE TABLE IF NOT EXISTS outbox (
 id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES sessions(id),
 input_id TEXT NOT NULL UNIQUE,
 run_id TEXT NOT NULL, target TEXT NOT NULL, text TEXT NOT NULL,
 status TEXT NOT NULL CHECK(status IN ('pending','sending','sent','unknown','failed'))
);
"""


class HostStore:
    def __init__(self, home: Path, *, defaults: HostSettings | None = None) -> None:
        self.home = Path(home).resolve()
        self.home.mkdir(parents=True, exist_ok=True)
        # A host owns one database and its JSONL directory; processes cannot independently
        # recover the same running inbox while another process is executing it.
        self._lease = (self.home / "host.lock").open("a+b")
        try:
            if sys.platform == "win32":
                import msvcrt

                self._lease.seek(0)
                msvcrt.locking(self._lease.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(self._lease.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self._lease.close()
            raise ConflictError("Application data directory already has a host") from exc
        try:
            # Bootstrap may construct on a different thread than the ASGI lifespan.
            # Once started, all operations belong to the host's single event loop.
            self._db = sqlite3.connect(
                self.home / "host.sqlite", isolation_level=None, check_same_thread=False
            )
            self._db.row_factory = sqlite3.Row
            self._db.execute("PRAGMA foreign_keys=ON")
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.execute("PRAGMA synchronous=FULL")
            version = self._db.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, 1, 2):
                raise ValueError("Unsupported host database version")
            self._db.executescript(_SCHEMA)
            self._db.executescript(PROVIDER_SCHEMA)
            self._db.execute("PRAGMA user_version=2")
            self.providers = ProviderStore(self._db)
            self._db.execute(
                "INSERT OR IGNORE INTO config VALUES(1,1,?)",
                ((defaults or HostSettings()).model_dump_json(),),
            )
            self._closed = False
        except BaseException:
            if hasattr(self, "_db"):
                self._db.close()
            self._lease.close()
            raise

    @contextmanager
    def transaction(self) -> Iterator[None]:
        self._db.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            self._db.execute("ROLLBACK")
            raise
        else:
            self._db.execute("COMMIT")

    def config(self) -> tuple[int, HostSettings]:
        row = self._db.execute("SELECT revision,data FROM config WHERE id=1").fetchone()
        return row[0], HostSettings.model_validate_json(row[1])

    def update_config(self, settings: HostSettings, revision: int) -> int:
        cursor = self._db.execute(
            "UPDATE config SET data=?,revision=revision+1 WHERE id=1 AND revision=?",
            (settings.model_dump_json(), revision),
        )
        if cursor.rowcount != 1:
            raise ConflictError("Configuration revision changed")
        return revision + 1

    @staticmethod
    def _session(row: sqlite3.Row) -> SessionRecord:
        return SessionRecord(
            row["id"],
            row["owner_id"],
            cast(Domain, row["domain"]),
            row["scope_id"],
            row["route_key"],
            row["title"],
            row["created_at"],
            row["updated_at"],
            SessionSettings.model_validate_json(row["settings"]),
            row["config_revision"],
        )

    def session(self, session_id: str) -> SessionRecord:
        row = self._db.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
        if row is None:
            raise NotFoundError("Session not found")
        return self._session(row)

    def sessions(self) -> list[SessionRecord]:
        return [
            self._session(row)
            for row in self._db.execute("SELECT * FROM sessions ORDER BY updated_at DESC,id")
        ]

    def create_session(
        self,
        *,
        owner_id: str,
        domain: Domain,
        title: str,
        request_id: str,
        settings: SessionSettings,
        route_key: str | None = None,
        validate_new: Callable[[SessionSettings], None] | None = None,
    ) -> SessionRecord:
        existing = self._db.execute(
            "SELECT * FROM sessions WHERE owner_id=? AND domain=? AND request_id=?",
            (owner_id, domain, request_id),
        ).fetchone()
        if existing is not None:
            if (
                existing["title"] != title
                or existing["domain"] != domain
                or existing["route_key"] != route_key
            ):
                raise ConflictError("Session creation request ID conflict")
            return self._session(existing)
        if route_key is not None:
            existing = self._db.execute(
                "SELECT * FROM sessions WHERE domain=? AND route_key=?", (domain, route_key)
            ).fetchone()
            if existing is not None:
                if existing["owner_id"] != owner_id:
                    raise ConflictError("Route is already owned by another principal")
                return self._session(existing)
        # Existing bindings and idempotent retries do not adopt today's defaults.
        # Validate only before the first insert, without yielding between lookup and write.
        if validate_new is not None:
            validate_new(settings)
        identity, scope, timestamp = uuid4().hex, uuid4().hex, now_ms()
        self._db.execute(
            "INSERT INTO sessions VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (
                identity,
                owner_id,
                domain,
                scope,
                route_key,
                title,
                timestamp,
                timestamp,
                settings.model_dump_json(),
                1,
                request_id,
            ),
        )
        return self.session(identity)

    def update_session(self, session_id: str, settings: SessionSettings, revision: int) -> int:
        cursor = self._db.execute(
            "UPDATE sessions SET settings=?,config_revision=config_revision+1,updated_at=? "
            "WHERE id=? AND config_revision=?",
            (settings.model_dump_json(), now_ms(), session_id, revision),
        )
        if cursor.rowcount != 1:
            raise ConflictError("Session configuration revision changed")
        return revision + 1

    @staticmethod
    def _input(row: sqlite3.Row) -> InputRecord:
        return InputRecord(**{key: row[key] for key in InputRecord.__dataclass_fields__})

    def input(self, input_id: str) -> InputRecord:
        row = self._db.execute("SELECT * FROM inputs WHERE id=?", (input_id,)).fetchone()
        if row is None:
            raise NotFoundError("Input not found")
        return self._input(row)

    def inputs(self, session_id: str) -> list[InputRecord]:
        return [
            self._input(row)
            for row in self._db.execute(
                "SELECT * FROM inputs WHERE session_id=? ORDER BY rowid", (session_id,)
            )
        ]

    def pending(self, session_id: str) -> list[InputRecord]:
        return [
            self._input(row)
            for row in self._db.execute(
                "SELECT * FROM inputs WHERE session_id=? AND status='pending' ORDER BY rowid",
                (session_id,),
            )
        ]

    def duplicate(self, session_id: str, request_id: str, key: str) -> InputRecord | None:
        rows = self._db.execute(
            "SELECT * FROM inputs WHERE session_id=? AND (request_id=? OR idempotency_key=?)",
            (session_id, request_id, key),
        ).fetchall()
        if len(rows) > 1:
            raise ConflictError("Request ID and idempotency key refer to different inputs")
        return self._input(rows[0]) if rows else None

    def enqueue(
        self,
        session: SessionRecord,
        *,
        text: str,
        request_id: str,
        key: str,
        kind: str = "prompt",
        logical_id: str | None = None,
        baseline_id: str | None = None,
        max_pending: int = 100,
    ) -> tuple[InputRecord, bool]:
        with self.transaction():
            duplicate = self.duplicate(session.id, request_id, key)
            if duplicate:
                if (duplicate.text, duplicate.kind) != (text, kind):
                    raise ConflictError("Idempotency key reused with different input")
                return duplicate, False
            if len(self.pending(session.id)) >= max_pending:
                raise BudgetExceeded("Session inbox is full")
            identity = uuid4().hex
            if logical_id is None:
                logical_id = identity
                settings = session.settings
                self._db.execute(
                    "INSERT INTO budgets VALUES(?,?,?,?,0,0,0)",
                    (
                        logical_id,
                        settings.max_requests_per_input,
                        settings.max_tokens_per_input,
                        settings.run_timeout_seconds * 1000,
                    ),
                )
            self._db.execute(
                "INSERT INTO inputs VALUES(?,?,?,?,?,?,?,'pending',?,NULL,NULL,?)",
                (
                    identity,
                    session.id,
                    request_id,
                    key,
                    text,
                    kind,
                    logical_id,
                    now_ms(),
                    baseline_id,
                ),
            )
            self._db.execute("UPDATE sessions SET updated_at=? WHERE id=?", (now_ms(), session.id))
        return self.input(identity), True

    def claim(self, input_id: str, *, baseline_id: str | None) -> InputRecord:
        cursor = self._db.execute(
            "UPDATE inputs SET status='running',run_id=?,baseline_id=? "
            "WHERE id=? AND status='pending'",
            (uuid4().hex, baseline_id, input_id),
        )
        if cursor.rowcount != 1:
            raise ConflictError("Input is no longer pending")
        return self.input(input_id)

    def finish(
        self,
        input_id: str,
        *,
        status: str,
        error: str | None = None,
        reply: tuple[str, str] | None = None,
    ) -> None:
        with self.transaction():
            item = self.input(input_id)
            self._db.execute(
                "UPDATE inputs SET status=?,error=? WHERE id=?", (status, error, input_id)
            )
            self._db.execute(
                "UPDATE sessions SET updated_at=? WHERE id=?", (now_ms(), item.session_id)
            )
            if reply is not None and item.run_id:
                target, text = reply
                self._db.execute(
                    "INSERT OR IGNORE INTO outbox VALUES(?,?,?,?,?,?,'pending')",
                    (uuid4().hex, item.session_id, item.id, item.run_id, target, text),
                )

    def cancel_pending(self, session_id: str) -> None:
        self._db.execute(
            "UPDATE inputs SET status='aborted' WHERE session_id=? AND status='pending'",
            (session_id,),
        )

    def recover(self) -> None:
        with self.transaction():
            # No replay after a crash. Queued but unclaimed work remains safe to start.
            self._db.execute(
                "UPDATE inputs SET status='unknown',error='Interrupted; not replayed' "
                "WHERE status='running'"
            )
            self._db.execute("UPDATE approvals SET status='expired' WHERE status='pending'")
            self._db.execute("UPDATE outbox SET status='unknown' WHERE status='sending'")

    def budget(self, logical_id: str) -> LogicalBudget:
        row = self._db.execute("SELECT * FROM budgets WHERE id=?", (logical_id,)).fetchone()
        if row is None:
            raise NotFoundError("Logical input budget not found")
        return LogicalBudget(**dict(row))

    def reserve_request(self, logical_id: str, estimated_input: int, max_output: int) -> int:
        with self.transaction():
            budget = self.budget(logical_id)
            if (
                budget.requests_used >= budget.max_requests
                or budget.milliseconds_used >= budget.max_milliseconds
            ):
                raise BudgetExceeded("Logical input request/time budget exhausted")
            available = budget.max_tokens - budget.tokens_used - estimated_input
            if available < 1:
                raise BudgetExceeded("Logical input token budget exhausted")
            output = min(available, max_output)
            # Conservative reservation is never refunded across crash/continuation.
            self._db.execute(
                "UPDATE budgets SET requests_used=requests_used+1,"
                "tokens_used=tokens_used+? WHERE id=?",
                (estimated_input + output, logical_id),
            )
        return output

    def charge_tokens(self, logical_id: str, tokens: int) -> None:
        self._db.execute(
            "UPDATE budgets SET tokens_used=tokens_used+? WHERE id=?", (max(0, tokens), logical_id)
        )

    def charge_time(self, logical_id: str, milliseconds: int) -> None:
        self._db.execute(
            "UPDATE budgets SET milliseconds_used=milliseconds_used+? WHERE id=?",
            (max(0, milliseconds), logical_id),
        )

    def create_approval(
        self,
        *,
        session_id: str,
        input_id: str,
        tool_name: str,
        arguments: dict[str, Any],
        expires_at: int,
    ) -> dict[str, Any]:
        identity = uuid4().hex
        self._db.execute(
            "INSERT INTO approvals VALUES(?,?,?,?,?,'pending',?,NULL)",
            (
                identity,
                session_id,
                input_id,
                tool_name,
                json.dumps(arguments, ensure_ascii=False, allow_nan=False),
                expires_at,
            ),
        )
        return self.approval(identity)

    def approval(self, identity: str) -> dict[str, Any]:
        row = self._db.execute("SELECT * FROM approvals WHERE id=?", (identity,)).fetchone()
        if row is None:
            raise NotFoundError("Approval not found")
        result = dict(row)
        result["arguments"] = json.loads(result["arguments"])
        result.pop("decision_by")
        return result

    def approvals(self, session_id: str) -> list[dict[str, Any]]:
        return [
            self.approval(row[0])
            for row in self._db.execute(
                "SELECT id FROM approvals WHERE session_id=? AND status='pending' ORDER BY rowid",
                (session_id,),
            )
        ]

    def decide_approval(
        self, identity: str, *, status: str, principal_id: str | None = None
    ) -> dict[str, Any]:
        row = self.approval(identity)
        if row["status"] != "pending":
            raise ConflictError("Approval is already settled")
        if row["expires_at"] <= now_ms() and status in ("approved", "denied"):
            self._db.execute("UPDATE approvals SET status='expired' WHERE id=?", (identity,))
            raise ConflictError("Approval expired")
        self._db.execute(
            "UPDATE approvals SET status=?,decision_by=? WHERE id=? AND status='pending'",
            (status, principal_id, identity),
        )
        return self.approval(identity)

    def outbox(self, session_id: str) -> list[dict[str, Any]]:
        return [
            dict(row)
            for row in self._db.execute("SELECT * FROM outbox WHERE session_id=?", (session_id,))
        ]

    def set_delivery(self, identity: str, *, expected: str, status: str) -> None:
        allowed = {
            ("pending", "sending"),
            ("sending", "sent"),
            ("sending", "unknown"),
            ("sending", "failed"),
        }
        if (expected, status) not in allowed:
            raise ValueError("Unsafe outbox transition")
        cursor = self._db.execute(
            "UPDATE outbox SET status=? WHERE id=? AND status=?", (status, identity, expected)
        )
        if cursor.rowcount != 1:
            raise ConflictError("Outbox state changed")

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            self._db.close()
            self._lease.close()
