"""Committed JSONL batches with checksums, explicit fsync and an OS writer lease.

A transaction is begin + entry records + commit. Only a matching commit is visible.
An incomplete/torn suffix is truncated on open; committed corruption is never repaired.
This is local single-writer storage, not a distributed lock or a zero-loss hardware promise.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
from collections.abc import Sequence
from copy import deepcopy
from pathlib import Path
from typing import Any, BinaryIO
from uuid import uuid4

from ..session.entries import SessionEntry, entry_data, parse_entry
from .base import (
    StoreBusyError,
    StoreCorruptionError,
    StoreError,
    StoreSnapshot,
    apply_batch,
    json_bytes,
)


def _lock(file: BinaryIO) -> None:
    try:
        if sys.platform == "win32":
            import msvcrt

            file.seek(0)
            msvcrt.locking(file.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        raise StoreBusyError("Session file already has a writer") from exc


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON key")
        result[key] = value
    return result


def _reject_constant(value: str) -> Any:
    raise ValueError(f"Invalid JSON constant: {value}")


class JsonlSessionStore:
    def __init__(self, path: Path, *, create: bool = False) -> None:
        self.path = Path(path)
        self._closed = False
        self._failed = False
        self.recovered_bytes = 0
        # Never create directories implicitly, overwrite a session, or use a fixed home.
        self._file = self.path.open("x+b" if create else "r+b")
        try:
            _lock(self._file)
            self._snapshot, self._committed_size = self._recover()
        except BaseException:
            self._file.close()
            self._closed = True
            raise

    def _require_open(self) -> None:
        if self._closed or self._failed:
            raise StoreError("Store is closed or failed; reopen it for recovery")

    def _recover(self) -> tuple[StoreSnapshot, int]:
        self._file.seek(0)
        data = self._file.read()
        lines = data.splitlines(keepends=True)
        state = StoreSnapshot()
        safe = 0
        offset = 0
        begin: dict[str, Any] | None = None
        pending: list[SessionEntry] = []
        digest = hashlib.sha256()
        for index, line in enumerate(lines):
            offset += len(line)
            if not line.endswith(b"\n"):
                # A commit without its final newline has not crossed the commit boundary.
                break
            try:
                record = json.loads(
                    line, object_pairs_hook=_unique_object, parse_constant=_reject_constant
                )
            except (ValueError, UnicodeDecodeError) as exc:
                if index == len(lines) - 1:
                    break
                raise StoreCorruptionError(f"Invalid JSON in middle at line {index + 1}") from exc
            try:
                if not isinstance(record, dict):
                    raise ValueError("Record must be an object")
                kind = record.get("record")
                if kind == "begin":
                    if begin is not None:
                        raise ValueError("Interleaved/uncommitted batch before another batch")
                    if set(record) != {"record", "tx", "revision", "count", "leaf_id"}:
                        raise ValueError("Invalid transaction header")
                    if (
                        not isinstance(record["tx"], str)
                        or not record["tx"]
                        or type(record["count"]) is not int
                        or record["count"] < 0
                        or type(record["revision"]) is not int
                        or record["revision"] != state.revision + 1
                        or not isinstance(record["leaf_id"], str)
                    ):
                        raise ValueError("Invalid transaction fields")
                    begin = record
                    pending = []
                    digest = hashlib.sha256(line)
                elif kind == "entry":
                    if (
                        begin is None
                        or set(record) != {"record", "tx", "entry"}
                        or record["tx"] != begin["tx"]
                        or len(pending) >= begin["count"]
                    ):
                        raise ValueError("Entry outside declared batch")
                    pending.append(parse_entry(record["entry"]))
                    digest.update(line)
                elif kind == "commit":
                    if (
                        begin is None
                        or set(record) != {"record", "tx", "digest"}
                        or record["tx"] != begin["tx"]
                        or record["digest"] != digest.hexdigest()
                        or len(pending) != begin["count"]
                    ):
                        raise ValueError("Invalid commit or checksum")
                    state = apply_batch(
                        state,
                        pending,
                        leaf_id=begin["leaf_id"],
                        expected_revision=state.revision,
                    )
                    safe = offset
                    begin = None
                    pending = []
                else:
                    raise ValueError(f"Unknown storage record: {kind!r}")
            except (ValueError, TypeError, KeyError) as exc:
                raise StoreCorruptionError(
                    f"Corrupt transaction at line {index + 1}: {exc}"
                ) from exc
        if safe != len(data):
            self.recovered_bytes = len(data) - safe
            self._file.seek(safe)
            self._file.truncate()
            self._sync()
        return state, safe

    def _sync(self) -> None:
        self._file.flush()
        os.fsync(self._file.fileno())

    def snapshot(self) -> StoreSnapshot:
        self._require_open()
        return deepcopy(self._snapshot)

    def commit(
        self,
        entries: Sequence[SessionEntry],
        *,
        leaf_id: str,
        expected_revision: int,
    ) -> StoreSnapshot:
        self._require_open()
        candidate = apply_batch(
            self._snapshot, entries, leaf_id=leaf_id, expected_revision=expected_revision
        )
        tx = uuid4().hex
        start = (
            json_bytes(
                {
                    "record": "begin",
                    "tx": tx,
                    "revision": candidate.revision,
                    "count": len(entries),
                    "leaf_id": leaf_id,
                }
            )
            + b"\n"
        )
        body = start + b"".join(
            json_bytes({"record": "entry", "tx": tx, "entry": entry_data(entry)}) + b"\n"
            for entry in candidate.entries[len(self._snapshot.entries) :]
        )
        commit = (
            json_bytes({"record": "commit", "tx": tx, "digest": hashlib.sha256(body).hexdigest()})
            + b"\n"
        )
        try:
            self._file.seek(self._committed_size)
            self._file.write(body + commit)
            self._sync()
        except BaseException:
            try:
                self._file.seek(self._committed_size)
                self._file.truncate()
                self._sync()
            except BaseException:
                self._failed = True
            raise
        self._committed_size += len(body) + len(commit)
        self._snapshot = candidate
        return self.snapshot()

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            self._file.close()  # The OS releases the writer lease, also after process death.

    def __enter__(self) -> JsonlSessionStore:
        self._require_open()
        return self

    def __exit__(self, *_: object) -> None:
        self.close()
