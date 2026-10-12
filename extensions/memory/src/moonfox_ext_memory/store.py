"""Bound, transactional SQLite memory storage with scope-composite constraints."""

from __future__ import annotations

import math
import sqlite3
import time
import unicodedata
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Literal
from uuid import uuid4

from moonfox_app.extension_api import ExtensionConflictError, ExtensionRecordNotFoundError

MemoryDomain = Literal["agent", "qq"]
MemoryKind = Literal["fact", "preference", "profile"]
MemoryOrigin = Literal["manual", "tool", "extraction"]
JobStatus = Literal["pending", "running", "done", "failed", "unknown", "cancelled"]

_SCHEMA: tuple[str, ...] = (
    """
    CREATE TABLE scope_state (
        domain TEXT NOT NULL CHECK (domain IN ('agent', 'qq')),
        scope_id TEXT NOT NULL CHECK (length(scope_id) > 0),
        revision INTEGER NOT NULL CHECK (revision >= 0),
        epoch INTEGER NOT NULL CHECK (epoch >= 0),
        PRIMARY KEY (domain, scope_id)
    ) WITHOUT ROWID
    """,
    """
    CREATE TABLE records (
        domain TEXT NOT NULL CHECK (domain IN ('agent', 'qq')),
        scope_id TEXT NOT NULL CHECK (length(scope_id) > 0),
        id TEXT NOT NULL CHECK (length(id) BETWEEN 1 AND 128),
        revision INTEGER NOT NULL CHECK (revision >= 1),
        text TEXT NOT NULL CHECK (
            length(text) BETWEEN 1 AND 2000 AND length(trim(text)) > 0
        ),
        kind TEXT NOT NULL CHECK (kind IN ('fact', 'preference', 'profile')),
        subject TEXT NOT NULL CHECK (length(trim(subject)) BETWEEN 1 AND 128),
        source_origin TEXT NOT NULL CHECK (
            source_origin IN ('manual', 'tool', 'extraction')
        ),
        source_entry_id TEXT NOT NULL DEFAULT '',
        confidence REAL NOT NULL CHECK (confidence >= 0.0 AND confidence <= 1.0),
        active INTEGER NOT NULL CHECK (active IN (0, 1)),
        created_at INTEGER NOT NULL,
        updated_at INTEGER NOT NULL,
        PRIMARY KEY (domain, scope_id, id),
        UNIQUE (
            domain, scope_id, text, kind, subject, source_origin, source_entry_id
        ),
        CHECK (
            (source_origin = 'manual' AND source_entry_id = '') OR
            (source_origin IN ('tool', 'extraction') AND length(source_entry_id) > 0)
        )
    ) WITHOUT ROWID
    """,
    """
    CREATE TABLE record_terms (
        domain TEXT NOT NULL CHECK (domain IN ('agent', 'qq')),
        scope_id TEXT NOT NULL CHECK (length(scope_id) > 0),
        record_id TEXT NOT NULL,
        term TEXT NOT NULL CHECK (length(term) BETWEEN 1 AND 2000),
        PRIMARY KEY (domain, scope_id, record_id, term),
        FOREIGN KEY (domain, scope_id, record_id)
            REFERENCES records (domain, scope_id, id) ON DELETE CASCADE
    ) WITHOUT ROWID
    """,
    """
    CREATE TABLE snapshots (
        domain TEXT NOT NULL CHECK (domain IN ('agent', 'qq')),
        scope_id TEXT NOT NULL CHECK (length(scope_id) > 0),
        id TEXT NOT NULL CHECK (length(id) BETWEEN 1 AND 128),
        message_id TEXT NOT NULL CHECK (length(message_id) BETWEEN 1 AND 256),
        view_revision INTEGER NOT NULL CHECK (view_revision >= 0),
        store_revision INTEGER NOT NULL CHECK (store_revision >= 0),
        context_text TEXT NOT NULL CHECK (
            length(context_text) BETWEEN 1 AND 50000
        ),
        created_at INTEGER NOT NULL,
        PRIMARY KEY (domain, scope_id, id)
    ) WITHOUT ROWID
    """,
    """
    CREATE TABLE snapshot_records (
        domain TEXT NOT NULL CHECK (domain IN ('agent', 'qq')),
        scope_id TEXT NOT NULL CHECK (length(scope_id) > 0),
        snapshot_id TEXT NOT NULL,
        record_id TEXT NOT NULL,
        record_revision INTEGER NOT NULL CHECK (record_revision >= 1),
        PRIMARY KEY (domain, scope_id, snapshot_id, record_id),
        FOREIGN KEY (domain, scope_id, snapshot_id)
            REFERENCES snapshots (domain, scope_id, id) ON DELETE CASCADE,
        FOREIGN KEY (domain, scope_id, record_id)
            REFERENCES records (domain, scope_id, id) ON DELETE CASCADE
    ) WITHOUT ROWID
    """,
    """
    CREATE TABLE jobs (
        domain TEXT NOT NULL CHECK (domain IN ('agent', 'qq')),
        scope_id TEXT NOT NULL CHECK (length(scope_id) > 0),
        id TEXT NOT NULL CHECK (length(id) BETWEEN 1 AND 128),
        source_entry_id TEXT NOT NULL CHECK (length(source_entry_id) BETWEEN 1 AND 256),
        extractor_version TEXT NOT NULL CHECK (
            length(extractor_version) BETWEEN 1 AND 64
        ),
        status TEXT NOT NULL CHECK (
            status IN ('pending', 'running', 'done', 'failed', 'unknown', 'cancelled')
        ),
        payload TEXT,
        budget_ticket TEXT CHECK (budget_ticket IS NULL OR length(budget_ticket) BETWEEN 1 AND 128),
        capture_epoch INTEGER NOT NULL CHECK (capture_epoch >= 0),
        created_at INTEGER NOT NULL,
        updated_at INTEGER NOT NULL,
        PRIMARY KEY (domain, scope_id, id),
        UNIQUE (domain, scope_id, source_entry_id, extractor_version),
        CHECK (
            (status IN ('pending', 'running') AND payload IS NOT NULL) OR
            (status NOT IN ('pending', 'running') AND payload IS NULL)
        ),
        CHECK (payload IS NULL OR length(payload) BETWEEN 1 AND 12000)
    ) WITHOUT ROWID
    """,
    "CREATE INDEX jobs_by_status ON jobs (domain, scope_id, status, created_at)",
)

_RECORD_COLUMNS = (
    "id, revision, text, kind, subject, source_origin, source_entry_id, "
    "confidence, active, created_at, updated_at"
)


@dataclass(frozen=True, slots=True)
class MemoryScope:
    """A single host-bound domain/scope pair; IDs remain opaque and nonempty."""

    domain: MemoryDomain
    scope_id: str

    def __post_init__(self) -> None:
        if self.domain not in ("agent", "qq"):
            raise ValueError("Memory domain must be 'agent' or 'qq'")
        if not isinstance(self.scope_id, str) or not self.scope_id.strip():
            raise ValueError("Memory scope_id must be a nonempty string")


@dataclass(frozen=True, slots=True)
class MemorySource:
    origin: MemoryOrigin
    entry_id: str | None

    def to_dict(self) -> dict[str, str | None]:
        return {"origin": self.origin, "entry_id": self.entry_id}


@dataclass(frozen=True, slots=True)
class MemoryRecord:
    id: str
    revision: int
    text: str
    kind: MemoryKind
    subject: str
    source: MemorySource
    confidence: float
    active: bool
    created_at: int
    updated_at: int

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "revision": self.revision,
            "text": self.text,
            "kind": self.kind,
            "subject": self.subject,
            "source": self.source.to_dict(),
            "confidence": self.confidence,
            "active": self.active,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


@dataclass(frozen=True, slots=True)
class MemoryCandidate:
    """Validated extraction fields only; source and scope are assigned by the store."""

    text: str
    kind: MemoryKind
    subject: str
    confidence: float


@dataclass(frozen=True, slots=True)
class MemoryJob:
    id: str
    source_entry_id: str
    extractor_version: str
    status: JobStatus
    created_at: int
    updated_at: int

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "source_entry_id": self.source_entry_id,
            "extractor_version": self.extractor_version,
            "status": self.status,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


@dataclass(frozen=True, slots=True)
class PendingExtraction:
    id: str
    source_entry_id: str
    extractor_version: str
    payload: str
    capture_epoch: int
    budget_ticket: str | None = None


@dataclass(frozen=True, slots=True)
class SnapshotReference:
    id: str
    revision: int


@dataclass(frozen=True, slots=True)
class MemorySnapshot:
    id: str
    message_id: str
    view_revision: int
    store_revision: int
    context_text: str
    created_at: int
    records: tuple[SnapshotReference, ...]


class ScopedMemoryStore:
    """SQLite store whose every data operation is bound to one immutable scope.

    The file can contain several test scopes, but no public read or mutation accepts a
    caller-provided domain or scope. WAL/FULL, foreign keys, and secure_delete are set
    for every connection. Transactions are synchronous and never span an await.
    """

    schema_version = 2
    snapshot_limit = 200

    def __init__(self, path: str | Path, scope: MemoryScope) -> None:
        if not isinstance(scope, MemoryScope):
            raise TypeError("scope must be MemoryScope")
        self._scope = scope
        self.path = Path(path)
        self._connection: sqlite3.Connection | None = None
        self._closed = False
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            connection = sqlite3.connect(
                self.path,
                timeout=5.0,
                isolation_level=None,
            )
            connection.row_factory = sqlite3.Row
            self._connection = connection
            connection.execute("PRAGMA busy_timeout = 5000")
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("PRAGMA secure_delete = ON")
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("PRAGMA synchronous = FULL")
            if int(connection.execute("PRAGMA foreign_keys").fetchone()[0]) != 1:
                raise RuntimeError("SQLite foreign-key enforcement is unavailable")
            if int(connection.execute("PRAGMA secure_delete").fetchone()[0]) != 1:
                raise RuntimeError("SQLite secure_delete is unavailable")
            self._migrate(connection)
            with self._transaction() as transaction:
                transaction.execute(
                    """
                    INSERT OR IGNORE INTO scope_state (domain, scope_id, revision, epoch)
                    VALUES (?, ?, 0, 0)
                    """,
                    self._scope_values,
                )
        except BaseException:
            self.close()
            raise

    @property
    def scope(self) -> MemoryScope:
        return self._scope

    @property
    def _scope_values(self) -> tuple[str, str]:
        return self.scope.domain, self.scope.scope_id

    @property
    def closed(self) -> bool:
        return self._closed

    def __enter__(self) -> ScopedMemoryStore:
        self._ensure_open()
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.close()

    def close(self) -> None:
        connection, self._connection = self._connection, None
        if connection is not None:
            connection.close()
        self._closed = True

    def _ensure_open(self) -> sqlite3.Connection:
        if self._closed or self._connection is None:
            raise RuntimeError("Memory store is closed")
        return self._connection

    def _migrate(self, connection: sqlite3.Connection) -> None:
        connection.execute("BEGIN IMMEDIATE")
        try:
            version = int(connection.execute("PRAGMA user_version").fetchone()[0])
            if version > self.schema_version:
                raise RuntimeError("Memory database schema is newer than this extension")
            if version == 0:
                for statement in _SCHEMA:
                    connection.execute(statement)
                connection.execute(f"PRAGMA user_version = {self.schema_version}")
            elif version == 1:
                connection.execute(
                    "ALTER TABLE jobs ADD COLUMN budget_ticket TEXT "
                    "CHECK (budget_ticket IS NULL OR length(budget_ticket) BETWEEN 1 AND 128)"
                )
                connection.execute(f"PRAGMA user_version = {self.schema_version}")
            elif version != self.schema_version:
                raise RuntimeError("Unsupported memory database schema")
            connection.commit()
        except BaseException:
            connection.rollback()
            raise

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        connection = self._ensure_open()
        connection.execute("BEGIN IMMEDIATE")
        try:
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise

    @contextmanager
    def _read_transaction(self) -> Iterator[sqlite3.Connection]:
        connection = self._ensure_open()
        connection.execute("BEGIN")
        try:
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise

    def scope_revision(self) -> int:
        connection = self._ensure_open()
        row = connection.execute(
            "SELECT revision FROM scope_state WHERE domain = ? AND scope_id = ?",
            self._scope_values,
        ).fetchone()
        if row is None:
            raise RuntimeError("Bound memory scope is missing")
        return int(row["revision"])

    def scope_epoch(self) -> int:
        connection = self._ensure_open()
        row = connection.execute(
            "SELECT epoch FROM scope_state WHERE domain = ? AND scope_id = ?",
            self._scope_values,
        ).fetchone()
        if row is None:
            raise RuntimeError("Bound memory scope is missing")
        return int(row["epoch"])

    def _bump_revision(self, connection: sqlite3.Connection) -> int:
        connection.execute(
            "UPDATE scope_state SET revision = revision + 1 WHERE domain = ? AND scope_id = ?",
            self._scope_values,
        )
        row = connection.execute(
            "SELECT revision FROM scope_state WHERE domain = ? AND scope_id = ?",
            self._scope_values,
        ).fetchone()
        if row is None:
            raise RuntimeError("Bound memory scope is missing")
        return int(row["revision"])

    @staticmethod
    def _now() -> int:
        return time.time_ns() // 1_000_000

    @staticmethod
    def _clean_text(value: str, *, maximum: int = 2000) -> str:
        if not isinstance(value, str):
            raise ValueError("Memory text must be a string")
        cleaned = value.strip()
        if not cleaned or "\x00" in cleaned or len(cleaned) > maximum:
            raise ValueError("Memory text must be nonblank and within its size limit")
        return cleaned

    @staticmethod
    def _clean_subject(value: str) -> str:
        if not isinstance(value, str):
            raise ValueError("Memory subject must be a string")
        cleaned = value.strip()
        if not cleaned or "\x00" in cleaned or len(cleaned) > 128:
            raise ValueError("Memory subject must be nonblank and at most 128 characters")
        return cleaned

    @staticmethod
    def _clean_entry_id(value: str | None, origin: MemoryOrigin) -> str:
        if origin == "manual":
            if value is not None:
                raise ValueError("Manual records do not have a chat entry id")
            return ""
        if not isinstance(value, str) or not value.strip() or len(value) > 256:
            raise ValueError("Tool and extraction sources require a bounded entry id")
        return value

    @staticmethod
    def _clean_kind(value: str) -> MemoryKind:
        if value not in ("fact", "preference", "profile"):
            raise ValueError("Unsupported memory kind")
        return value  # type: ignore[return-value]

    @staticmethod
    def _clean_origin(value: str) -> MemoryOrigin:
        if value not in ("manual", "tool", "extraction"):
            raise ValueError("Unsupported memory source")
        return value  # type: ignore[return-value]

    @staticmethod
    def _clean_confidence(value: float) -> float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("Memory confidence must be a finite number")
        confidence = float(value)
        if not math.isfinite(confidence) or not 0.0 <= confidence <= 1.0:
            raise ValueError("Memory confidence must be between 0 and 1")
        return confidence

    @staticmethod
    def _clean_revision(value: int, *, allow_zero: bool = False) -> int:
        minimum = 0 if allow_zero else 1
        if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
            raise ValueError("Revision must be a nonnegative integer")
        return value

    @staticmethod
    def _is_cjk(character: str) -> bool:
        codepoint = ord(character)
        return (
            0x3400 <= codepoint <= 0x4DBF
            or 0x4E00 <= codepoint <= 0x9FFF
            or 0xF900 <= codepoint <= 0xFAFF
            or 0x20000 <= codepoint <= 0x3134F
            or 0x3040 <= codepoint <= 0x30FF
            or 0xAC00 <= codepoint <= 0xD7AF
        )

    @classmethod
    def _terms(cls, text: str) -> set[str]:
        normalized = unicodedata.normalize("NFKC", text).casefold()
        terms: set[str] = set()
        latin: list[str] = []
        cjk: list[str] = []

        def flush_latin() -> None:
            if latin:
                terms.add("".join(latin))
                latin.clear()

        def flush_cjk() -> None:
            if not cjk:
                return
            run = "".join(cjk)
            if len(run) == 1:
                terms.add(run)
            else:
                for width in (2, 3):
                    for index in range(len(run) - width + 1):
                        terms.add(run[index : index + width])
            cjk.clear()

        for character in normalized:
            if cls._is_cjk(character):
                flush_latin()
                cjk.append(character)
            elif character.isalpha() or character.isdigit() or character == "_":
                flush_cjk()
                latin.append(character)
            else:
                flush_latin()
                flush_cjk()
        flush_latin()
        flush_cjk()
        return terms

    @classmethod
    def _query_terms(cls, query: str) -> tuple[str, ...]:
        if not isinstance(query, str) or len(query) > 1000:
            raise ValueError("Memory query must be a string of at most 1000 characters")
        terms = tuple(sorted(cls._terms(query)))
        if len(terms) > 900:
            raise ValueError("Memory query contains too many distinct search terms")
        return terms

    def _row_record(self, row: sqlite3.Row) -> MemoryRecord:
        entry_id = str(row["source_entry_id"])
        source: MemorySource = MemorySource(
            origin=row["source_origin"],
            entry_id=entry_id or None,
        )
        return MemoryRecord(
            id=str(row["id"]),
            revision=int(row["revision"]),
            text=str(row["text"]),
            kind=row["kind"],
            subject=str(row["subject"]),
            source=source,
            confidence=float(row["confidence"]),
            active=bool(row["active"]),
            created_at=int(row["created_at"]),
            updated_at=int(row["updated_at"]),
        )

    def _record_by_id(self, connection: sqlite3.Connection, record_id: str) -> MemoryRecord | None:
        row = connection.execute(
            f"SELECT {_RECORD_COLUMNS} FROM records WHERE domain = ? AND scope_id = ? AND id = ?",
            (*self._scope_values, record_id),
        ).fetchone()
        return None if row is None else self._row_record(row)

    def _record_by_unique(
        self,
        connection: sqlite3.Connection,
        *,
        text: str,
        kind: MemoryKind,
        subject: str,
        origin: MemoryOrigin,
        entry_id: str,
    ) -> MemoryRecord | None:
        row = connection.execute(
            f"SELECT {_RECORD_COLUMNS} FROM records "
            "WHERE domain = ? AND scope_id = ? AND text = ? AND kind = ? "
            "AND subject = ? AND source_origin = ? AND source_entry_id = ?",
            (*self._scope_values, text, kind, subject, origin, entry_id),
        ).fetchone()
        return None if row is None else self._row_record(row)

    def _insert_record(
        self,
        connection: sqlite3.Connection,
        *,
        text: str,
        kind: MemoryKind,
        subject: str,
        origin: MemoryOrigin,
        entry_id: str,
        confidence: float,
    ) -> tuple[MemoryRecord, bool]:
        existing = self._record_by_unique(
            connection,
            text=text,
            kind=kind,
            subject=subject,
            origin=origin,
            entry_id=entry_id,
        )
        if existing is not None:
            return existing, False
        record_id = uuid4().hex
        now = self._now()
        connection.execute(
            """
            INSERT INTO records (
                domain, scope_id, id, revision, text, kind, subject,
                source_origin, source_entry_id, confidence, active, created_at, updated_at
            ) VALUES (?, ?, ?, 1, ?, ?, ?, ?, ?, ?, 1, ?, ?)
            """,
            (
                *self._scope_values,
                record_id,
                text,
                kind,
                subject,
                origin,
                entry_id,
                confidence,
                now,
                now,
            ),
        )
        scope_values = self._scope_values
        terms = {term for indexed_text in (text, subject) for term in self._terms(indexed_text)}
        connection.executemany(
            "INSERT OR IGNORE INTO record_terms (domain, scope_id, record_id, term) "
            "VALUES (?, ?, ?, ?)",
            ((*scope_values, record_id, term) for term in terms),
        )
        inserted = self._record_by_id(connection, record_id)
        if inserted is None:
            raise RuntimeError("Inserted memory record could not be read")
        return inserted, True

    def remember(
        self,
        text: str,
        kind: MemoryKind = "fact",
        subject: str = "conversation",
        *,
        origin: MemoryOrigin = "manual",
        entry_id: str | None = None,
        confidence: float = 1.0,
    ) -> MemoryRecord:
        cleaned_text = self._clean_text(text)
        cleaned_kind = self._clean_kind(kind)
        cleaned_subject = self._clean_subject(subject)
        cleaned_origin = self._clean_origin(origin)
        cleaned_entry_id = self._clean_entry_id(entry_id, cleaned_origin)
        cleaned_confidence = self._clean_confidence(confidence)
        with self._transaction() as connection:
            record, inserted = self._insert_record(
                connection,
                text=cleaned_text,
                kind=cleaned_kind,
                subject=cleaned_subject,
                origin=cleaned_origin,
                entry_id=cleaned_entry_id,
                confidence=cleaned_confidence,
            )
            if inserted:
                self._bump_revision(connection)
            return record

    def get(self, record_id: str) -> MemoryRecord:
        if not isinstance(record_id, str) or not record_id or len(record_id) > 128:
            raise ValueError("Record id must be a nonempty string of at most 128 characters")
        connection = self._ensure_open()
        record = self._record_by_id(connection, record_id)
        if record is None:
            raise ExtensionRecordNotFoundError("Memory record was not found in this scope")
        return record

    @staticmethod
    def _query_rows(
        connection: sqlite3.Connection,
        scope_values: tuple[str, str],
        terms: tuple[str, ...],
        limit: int,
        *,
        include_forgotten: bool,
    ) -> tuple[sqlite3.Row, ...]:
        active_clause = "" if include_forgotten else "AND r.active = 1"
        if not terms:
            rows = connection.execute(
                f"SELECT {_RECORD_COLUMNS} FROM records r "
                "WHERE r.domain = ? AND r.scope_id = ? "
                f"{active_clause} ORDER BY r.updated_at DESC, r.id ASC LIMIT ?",
                (*scope_values, limit),
            ).fetchall()
            return tuple(rows)
        placeholders = ", ".join("?" for _ in terms)
        rows = connection.execute(
            f"""
            SELECT r.id, r.revision, r.text, r.kind, r.subject, r.source_origin,
                   r.source_entry_id, r.confidence, r.active, r.created_at, r.updated_at
            FROM records AS r
            JOIN (
                SELECT domain, scope_id, record_id, COUNT(DISTINCT term) AS score
                FROM record_terms
                WHERE domain = ? AND scope_id = ? AND term IN ({placeholders})
                GROUP BY domain, scope_id, record_id
            ) AS matched
              ON matched.domain = r.domain
             AND matched.scope_id = r.scope_id
             AND matched.record_id = r.id
            WHERE r.domain = ? AND r.scope_id = ? {active_clause}
            ORDER BY matched.score DESC, r.updated_at DESC, r.id ASC
            LIMIT ?
            """,
            (*scope_values, *terms, *scope_values, limit),
        ).fetchall()
        return tuple(rows)

    def search_with_revision(
        self, query: str, limit: int = 6
    ) -> tuple[int, tuple[MemoryRecord, ...]]:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise ValueError("Search limit must be between 1 and 100")
        terms = self._query_terms(query)
        with self._read_transaction() as connection:
            state = connection.execute(
                "SELECT revision FROM scope_state WHERE domain = ? AND scope_id = ?",
                self._scope_values,
            ).fetchone()
            if state is None:
                raise RuntimeError("Bound memory scope is missing")
            if not terms:
                return int(state["revision"]), ()
            rows = self._query_rows(
                connection,
                self._scope_values,
                terms,
                limit,
                include_forgotten=False,
            )
            return int(state["revision"]), tuple(self._row_record(row) for row in rows)

    def search(self, query: str, limit: int = 6) -> tuple[MemoryRecord, ...]:
        return self.search_with_revision(query, limit)[1]

    def list_with_revision(
        self,
        query: str = "",
        limit: int = 50,
        *,
        include_forgotten: bool = False,
    ) -> tuple[int, tuple[MemoryRecord, ...]]:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise ValueError("List limit must be between 1 and 100")
        if not isinstance(include_forgotten, bool):
            raise ValueError("include_forgotten must be a boolean")
        has_query = bool(query.strip()) if isinstance(query, str) else True
        terms = self._query_terms(query)
        with self._read_transaction() as connection:
            state = connection.execute(
                "SELECT revision FROM scope_state WHERE domain = ? AND scope_id = ?",
                self._scope_values,
            ).fetchone()
            if state is None:
                raise RuntimeError("Bound memory scope is missing")
            if has_query and not terms:
                return int(state["revision"]), ()
            rows = self._query_rows(
                connection,
                self._scope_values,
                terms if has_query else (),
                limit,
                include_forgotten=include_forgotten,
            )
            return int(state["revision"]), tuple(self._row_record(row) for row in rows)

    def list_records(
        self,
        query: str = "",
        limit: int = 50,
        *,
        include_forgotten: bool = False,
    ) -> tuple[MemoryRecord, ...]:
        return self.list_with_revision(query, limit, include_forgotten=include_forgotten)[1]

    def update(
        self,
        record_id: str,
        expected_revision: int,
        text: str,
        kind: MemoryKind = "fact",
        subject: str = "conversation",
    ) -> MemoryRecord:
        expected = self._clean_revision(expected_revision)
        cleaned_text = self._clean_text(text)
        cleaned_kind = self._clean_kind(kind)
        cleaned_subject = self._clean_subject(subject)
        now = self._now()
        with self._transaction() as connection:
            current = self._record_by_id(connection, record_id)
            if current is None:
                raise ExtensionRecordNotFoundError("Memory record was not found in this scope")
            if current.revision != expected:
                raise ExtensionConflictError("Memory record revision changed")
            connection.execute(
                """
                UPDATE records
                SET revision = revision + 1, text = ?, kind = ?, subject = ?, updated_at = ?
                WHERE domain = ? AND scope_id = ? AND id = ? AND revision = ?
                """,
                (
                    cleaned_text,
                    cleaned_kind,
                    cleaned_subject,
                    now,
                    *self._scope_values,
                    record_id,
                    expected,
                ),
            )
            connection.execute(
                "DELETE FROM record_terms WHERE domain = ? AND scope_id = ? AND record_id = ?",
                (*self._scope_values, record_id),
            )
            terms = {
                term
                for indexed_text in (cleaned_text, cleaned_subject)
                for term in self._terms(indexed_text)
            }
            connection.executemany(
                "INSERT OR IGNORE INTO record_terms (domain, scope_id, record_id, term) "
                "VALUES (?, ?, ?, ?)",
                ((*self._scope_values, record_id, term) for term in terms),
            )
            connection.execute(
                "DELETE FROM snapshots WHERE domain = ? AND scope_id = ?",
                self._scope_values,
            )
            self._bump_revision(connection)
            updated = self._record_by_id(connection, record_id)
            if updated is None:
                raise RuntimeError("Updated memory record could not be read")
            return updated

    def _invalidate_pending_jobs(self, connection: sqlite3.Connection) -> None:
        now = self._now()
        connection.execute(
            """
            UPDATE jobs SET status = 'cancelled', payload = NULL, updated_at = ?
            WHERE domain = ? AND scope_id = ? AND status IN ('pending', 'running')
            """,
            (now, *self._scope_values),
        )
        connection.execute(
            "UPDATE scope_state SET epoch = epoch + 1 WHERE domain = ? AND scope_id = ?",
            self._scope_values,
        )

    def forget(self, record_id: str, expected_revision: int) -> MemoryRecord:
        expected = self._clean_revision(expected_revision)
        now = self._now()
        with self._transaction() as connection:
            current = self._record_by_id(connection, record_id)
            if current is None:
                raise ExtensionRecordNotFoundError("Memory record was not found in this scope")
            if current.revision != expected:
                raise ExtensionConflictError("Memory record revision changed")
            connection.execute(
                """
                UPDATE records SET active = 0, revision = revision + 1, updated_at = ?
                WHERE domain = ? AND scope_id = ? AND id = ? AND revision = ?
                """,
                (now, *self._scope_values, record_id, expected),
            )
            self._invalidate_pending_jobs(connection)
            connection.execute(
                "DELETE FROM snapshots WHERE domain = ? AND scope_id = ?",
                self._scope_values,
            )
            self._bump_revision(connection)
            forgotten = self._record_by_id(connection, record_id)
            if forgotten is None:
                raise RuntimeError("Forgotten memory record could not be read")
            return forgotten

    def delete(self, record_id: str, expected_revision: int) -> bool:
        expected = self._clean_revision(expected_revision)
        with self._transaction() as connection:
            current = self._record_by_id(connection, record_id)
            if current is None:
                raise ExtensionRecordNotFoundError("Memory record was not found in this scope")
            if current.revision != expected:
                raise ExtensionConflictError("Memory record revision changed")
            self._invalidate_pending_jobs(connection)
            connection.execute(
                "DELETE FROM snapshots WHERE domain = ? AND scope_id = ?",
                self._scope_values,
            )
            connection.execute(
                "DELETE FROM records WHERE domain = ? AND scope_id = ? AND id = ? AND revision = ?",
                (*self._scope_values, record_id, expected),
            )
            self._bump_revision(connection)
            return True

    def clear(self, expected_revision: int) -> int:
        expected = self._clean_revision(expected_revision, allow_zero=True)
        with self._transaction() as connection:
            state = connection.execute(
                "SELECT revision FROM scope_state WHERE domain = ? AND scope_id = ?",
                self._scope_values,
            ).fetchone()
            if state is None:
                raise RuntimeError("Bound memory scope is missing")
            if int(state["revision"]) != expected:
                raise ExtensionConflictError("Memory scope revision changed")
            self._invalidate_pending_jobs(connection)
            connection.execute(
                "DELETE FROM snapshots WHERE domain = ? AND scope_id = ?",
                self._scope_values,
            )
            connection.execute(
                "DELETE FROM records WHERE domain = ? AND scope_id = ?",
                self._scope_values,
            )
            return self._bump_revision(connection)

    def queue_extraction(
        self,
        source_entry_id: str,
        payload: str,
        extractor_version: str,
        *,
        budget_ticket: str | None = None,
    ) -> tuple[MemoryJob, bool]:
        entry_id = self._clean_entry_id(source_entry_id, "extraction")
        cleaned_payload = self._clean_text(payload, maximum=12000)
        if not isinstance(extractor_version, str) or not extractor_version.strip():
            raise ValueError("Extractor version must be nonempty")
        if len(extractor_version) > 64:
            raise ValueError("Extractor version is too long")
        if budget_ticket is not None and (
            not isinstance(budget_ticket, str)
            or not budget_ticket.strip()
            or len(budget_ticket) > 128
        ):
            raise ValueError("Invalid originating input budget ticket")
        with self._transaction() as connection:
            state = connection.execute(
                "SELECT epoch FROM scope_state WHERE domain = ? AND scope_id = ?",
                self._scope_values,
            ).fetchone()
            if state is None:
                raise RuntimeError("Bound memory scope is missing")
            existing = connection.execute(
                """
                SELECT id, source_entry_id, extractor_version, status, created_at, updated_at
                FROM jobs WHERE domain = ? AND scope_id = ?
                  AND source_entry_id = ? AND extractor_version = ?
                """,
                (*self._scope_values, entry_id, extractor_version),
            ).fetchone()
            if existing is not None:
                return self._row_job(existing), False
            now = self._now()
            job_id = uuid4().hex
            connection.execute(
                """
                INSERT INTO jobs (
                    domain, scope_id, id, source_entry_id, extractor_version,
                    status, payload, budget_ticket, capture_epoch, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?, ?)
                """,
                (
                    *self._scope_values,
                    job_id,
                    entry_id,
                    extractor_version,
                    cleaned_payload,
                    budget_ticket,
                    int(state["epoch"]),
                    now,
                    now,
                ),
            )
            self._bump_revision(connection)
            job = connection.execute(
                """
                SELECT id, source_entry_id, extractor_version, status, created_at, updated_at
                FROM jobs WHERE domain = ? AND scope_id = ? AND id = ?
                """,
                (*self._scope_values, job_id),
            ).fetchone()
            if job is None:
                raise RuntimeError("Queued extraction job could not be read")
            return self._row_job(job), True

    @staticmethod
    def _row_job(row: sqlite3.Row) -> MemoryJob:
        return MemoryJob(
            id=str(row["id"]),
            source_entry_id=str(row["source_entry_id"]),
            extractor_version=str(row["extractor_version"]),
            status=row["status"],
            created_at=int(row["created_at"]),
            updated_at=int(row["updated_at"]),
        )

    def list_jobs(self, limit: int = 100) -> tuple[MemoryJob, ...]:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 500:
            raise ValueError("Job list limit must be between 1 and 500")
        connection = self._ensure_open()
        rows = connection.execute(
            """
            SELECT id, source_entry_id, extractor_version, status, created_at, updated_at
            FROM jobs WHERE domain = ? AND scope_id = ?
            ORDER BY created_at DESC, id ASC LIMIT ?
            """,
            (*self._scope_values, limit),
        ).fetchall()
        return tuple(self._row_job(row) for row in rows)

    def find_job_for_entry(self, source_entry_id: str, extractor_version: str) -> MemoryJob | None:
        connection = self._ensure_open()
        row = connection.execute(
            """
            SELECT id, source_entry_id, extractor_version, status, created_at, updated_at
            FROM jobs WHERE domain = ? AND scope_id = ? AND source_entry_id = ?
              AND extractor_version = ?
            """,
            (*self._scope_values, source_entry_id, extractor_version),
        ).fetchone()
        return None if row is None else self._row_job(row)

    def pending_job_for_entry(
        self, source_entry_id: str, extractor_version: str
    ) -> PendingExtraction | None:
        connection = self._ensure_open()
        row = connection.execute(
            """
            SELECT id, source_entry_id, extractor_version, payload, capture_epoch, budget_ticket
            FROM jobs WHERE domain = ? AND scope_id = ? AND source_entry_id = ?
              AND extractor_version = ? AND status = 'pending'
            """,
            (*self._scope_values, source_entry_id, extractor_version),
        ).fetchone()
        if row is None:
            return None
        payload = row["payload"]
        if not isinstance(payload, str):
            return None
        return PendingExtraction(
            id=str(row["id"]),
            source_entry_id=str(row["source_entry_id"]),
            extractor_version=str(row["extractor_version"]),
            payload=payload,
            capture_epoch=int(row["capture_epoch"]),
            budget_ticket=row["budget_ticket"],
        )

    def pending_extractions(
        self, extractor_version: str, *, limit: int = 4
    ) -> tuple[PendingExtraction, ...]:
        if type(limit) is not int or not 1 <= limit <= 64:
            raise ValueError("Pending extraction window must be between 1 and 64")
        rows = (
            self._ensure_open()
            .execute(
                "SELECT source_entry_id FROM jobs WHERE domain=? AND scope_id=? "
                "AND extractor_version=? AND status='pending' ORDER BY created_at,id LIMIT ?",
                (*self._scope_values, extractor_version, limit),
            )
            .fetchall()
        )
        return tuple(
            pending
            for row in rows
            if (pending := self.pending_job_for_entry(str(row[0]), extractor_version)) is not None
        )

    def recover_running_jobs(self) -> int:
        with self._transaction() as connection:
            rows = connection.execute(
                "SELECT id FROM jobs WHERE domain = ? AND scope_id = ? AND status = 'running'",
                self._scope_values,
            ).fetchall()
            if not rows:
                return 0
            connection.execute(
                """
                UPDATE jobs SET status = 'unknown', payload = NULL, updated_at = ?
                WHERE domain = ? AND scope_id = ? AND status = 'running'
                """,
                (self._now(), *self._scope_values),
            )
            self._bump_revision(connection)
            return len(rows)

    def claim_job(self, job_id: str, capture_epoch: int) -> PendingExtraction | None:
        with self._transaction() as connection:
            state = connection.execute(
                "SELECT epoch FROM scope_state WHERE domain = ? AND scope_id = ?",
                self._scope_values,
            ).fetchone()
            if state is None or int(state["epoch"]) != capture_epoch:
                return None
            row = connection.execute(
                """
                SELECT id, source_entry_id, extractor_version, payload, capture_epoch, budget_ticket
                FROM jobs WHERE domain = ? AND scope_id = ? AND id = ? AND status = 'pending'
                """,
                (*self._scope_values, job_id),
            ).fetchone()
            if row is None or int(row["capture_epoch"]) != capture_epoch:
                return None
            payload = row["payload"]
            if not isinstance(payload, str):
                return None
            changed = connection.execute(
                """
                UPDATE jobs SET status = 'running', updated_at = ?
                WHERE domain = ? AND scope_id = ? AND id = ? AND status = 'pending'
                  AND capture_epoch = ?
                """,
                (self._now(), *self._scope_values, job_id, capture_epoch),
            ).rowcount
            if changed != 1:
                return None
            self._bump_revision(connection)
            return PendingExtraction(
                id=str(row["id"]),
                source_entry_id=str(row["source_entry_id"]),
                extractor_version=str(row["extractor_version"]),
                payload=payload,
                capture_epoch=capture_epoch,
                budget_ticket=row["budget_ticket"],
            )

    def mark_job_unknown(self, job_id: str, capture_epoch: int) -> bool:
        return self._finish_without_candidates(job_id, capture_epoch, "unknown")

    def mark_job_failed(self, job_id: str, capture_epoch: int) -> bool:
        return self._finish_without_candidates(job_id, capture_epoch, "failed")

    def _finish_without_candidates(
        self, job_id: str, capture_epoch: int, status: Literal["failed", "unknown"]
    ) -> bool:
        with self._transaction() as connection:
            state = connection.execute(
                "SELECT epoch FROM scope_state WHERE domain = ? AND scope_id = ?",
                self._scope_values,
            ).fetchone()
            if state is None or int(state["epoch"]) != capture_epoch:
                return False
            changed = connection.execute(
                """
                UPDATE jobs SET status = ?, payload = NULL, updated_at = ?
                WHERE domain = ? AND scope_id = ? AND id = ? AND status = 'running'
                  AND capture_epoch = ?
                """,
                (status, self._now(), *self._scope_values, job_id, capture_epoch),
            ).rowcount
            if changed:
                self._bump_revision(connection)
            return changed == 1

    def complete_job(
        self,
        job_id: str,
        capture_epoch: int,
        candidates: Sequence[MemoryCandidate],
    ) -> bool:
        if len(candidates) > 8:
            raise ValueError("At most eight extraction candidates are accepted")
        cleaned: list[MemoryCandidate] = []
        for candidate in candidates:
            if not isinstance(candidate, MemoryCandidate):
                raise ValueError("Extraction candidates must be validated records")
            text = self._clean_text(candidate.text)
            kind = self._clean_kind(candidate.kind)
            subject = self._clean_subject(candidate.subject)
            if subject != "conversation":
                raise ValueError("Extracted memory subject must be conversation")
            confidence = min(self._clean_confidence(candidate.confidence), 0.6)
            cleaned.append(MemoryCandidate(text, kind, subject, confidence))
        with self._transaction() as connection:
            state = connection.execute(
                "SELECT epoch FROM scope_state WHERE domain = ? AND scope_id = ?",
                self._scope_values,
            ).fetchone()
            if state is None or int(state["epoch"]) != capture_epoch:
                return False
            job = connection.execute(
                """
                SELECT source_entry_id FROM jobs
                WHERE domain = ? AND scope_id = ? AND id = ? AND status = 'running'
                  AND capture_epoch = ?
                """,
                (*self._scope_values, job_id, capture_epoch),
            ).fetchone()
            if job is None:
                return False
            source_entry_id = str(job["source_entry_id"])
            inserted_any = False
            for candidate in cleaned:
                _, inserted = self._insert_record(
                    connection,
                    text=candidate.text,
                    kind=candidate.kind,
                    subject=candidate.subject,
                    origin="extraction",
                    entry_id=source_entry_id,
                    confidence=candidate.confidence,
                )
                inserted_any = inserted_any or inserted
            connection.execute(
                """
                UPDATE jobs SET status = 'done', payload = NULL, updated_at = ?
                WHERE domain = ? AND scope_id = ? AND id = ? AND status = 'running'
                  AND capture_epoch = ?
                """,
                (self._now(), *self._scope_values, job_id, capture_epoch),
            )
            if inserted_any:
                connection.execute(
                    "DELETE FROM snapshots WHERE domain = ? AND scope_id = ?",
                    self._scope_values,
                )
            self._bump_revision(connection)
            return True

    def save_snapshot(
        self,
        snapshot_id: str,
        message_id: str,
        view_revision: int,
        store_revision: int,
        references: Sequence[tuple[str, int]],
        context_text: str,
    ) -> bool:
        if not isinstance(snapshot_id, str) or not snapshot_id or len(snapshot_id) > 128:
            raise ValueError("Snapshot id must be nonempty and bounded")
        if not isinstance(message_id, str) or not message_id or len(message_id) > 256:
            raise ValueError("Snapshot message id must be nonempty and bounded")
        view_rev = self._clean_revision(view_revision, allow_zero=True)
        revision = self._clean_revision(store_revision, allow_zero=True)
        text = self._clean_text(context_text, maximum=50000)
        cleaned_references: list[tuple[str, int]] = []
        for record_id, record_revision in references:
            if not isinstance(record_id, str) or not record_id or len(record_id) > 128:
                raise ValueError("Snapshot record id must be nonempty and bounded")
            cleaned_references.append((record_id, self._clean_revision(record_revision)))
        if not cleaned_references:
            raise ValueError("Snapshots must reference at least one selected record")
        now = self._now()
        with self._transaction() as connection:
            state = connection.execute(
                "SELECT revision FROM scope_state WHERE domain = ? AND scope_id = ?",
                self._scope_values,
            ).fetchone()
            if state is None or int(state["revision"]) != revision:
                return False
            for record_id, record_revision in cleaned_references:
                row = connection.execute(
                    """
                    SELECT revision, active FROM records
                    WHERE domain = ? AND scope_id = ? AND id = ?
                    """,
                    (*self._scope_values, record_id),
                ).fetchone()
                if (
                    row is None
                    or int(row["revision"]) != record_revision
                    or not bool(row["active"])
                ):
                    return False
            connection.execute(
                """
                INSERT INTO snapshots (
                    domain, scope_id, id, message_id, view_revision,
                    store_revision, context_text, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (domain, scope_id, id) DO UPDATE SET
                    message_id = excluded.message_id,
                    view_revision = excluded.view_revision,
                    store_revision = excluded.store_revision,
                    context_text = excluded.context_text,
                    created_at = excluded.created_at
                """,
                (*self._scope_values, snapshot_id, message_id, view_rev, revision, text, now),
            )
            connection.execute(
                "DELETE FROM snapshot_records WHERE domain = ? AND scope_id = "
                "? AND snapshot_id = ?",
                (*self._scope_values, snapshot_id),
            )
            connection.executemany(
                """
                INSERT INTO snapshot_records (
                    domain, scope_id, snapshot_id, record_id, record_revision
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    (*self._scope_values, snapshot_id, record_id, record_revision)
                    for record_id, record_revision in cleaned_references
                ),
            )
            connection.execute(
                """
                DELETE FROM snapshots
                WHERE domain = ? AND scope_id = ? AND id IN (
                    SELECT id FROM snapshots
                    WHERE domain = ? AND scope_id = ? AND id <> ?
                    ORDER BY created_at DESC, id DESC
                    LIMIT -1 OFFSET ?
                )
                """,
                (*self._scope_values, *self._scope_values, snapshot_id, self.snapshot_limit - 1),
            )
            return True

    def snapshots(self, limit: int = 200) -> tuple[MemorySnapshot, ...]:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 200:
            raise ValueError("Snapshot limit must be between 1 and 200")
        connection = self._ensure_open()
        rows = connection.execute(
            """
            SELECT id, message_id, view_revision, store_revision, context_text, created_at
            FROM snapshots WHERE domain = ? AND scope_id = ?
            ORDER BY created_at DESC, id ASC LIMIT ?
            """,
            (*self._scope_values, limit),
        ).fetchall()
        snapshots: list[MemorySnapshot] = []
        for row in rows:
            references = connection.execute(
                """
                SELECT record_id, record_revision FROM snapshot_records
                WHERE domain = ? AND scope_id = ? AND snapshot_id = ?
                ORDER BY record_id
                """,
                (*self._scope_values, str(row["id"])),
            ).fetchall()
            snapshots.append(
                MemorySnapshot(
                    id=str(row["id"]),
                    message_id=str(row["message_id"]),
                    view_revision=int(row["view_revision"]),
                    store_revision=int(row["store_revision"]),
                    context_text=str(row["context_text"]),
                    created_at=int(row["created_at"]),
                    records=tuple(
                        SnapshotReference(str(ref["record_id"]), int(ref["record_revision"]))
                        for ref in references
                    ),
                )
            )
        return tuple(snapshots)
