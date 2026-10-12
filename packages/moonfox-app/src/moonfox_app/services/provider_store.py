"""Provider/model rows in the host database. No clear-text credential column."""

import sqlite3
from dataclasses import dataclass, field

from moonfox_app.config.providers import ModelSettings, ProviderSettings

from .errors import ConflictError, NotFoundError

SCHEMA = """
CREATE TABLE IF NOT EXISTS providers (
 id TEXT PRIMARY KEY, revision INTEGER NOT NULL, settings TEXT NOT NULL,
 secret BLOB, secret_format TEXT
);
CREATE TABLE IF NOT EXISTS models (
 id TEXT PRIMARY KEY, provider_id TEXT NOT NULL REFERENCES providers(id),
 revision INTEGER NOT NULL, settings TEXT NOT NULL
);
"""


@dataclass(frozen=True, slots=True)
class ProviderRecord:
    id: str
    revision: int
    settings: ProviderSettings
    secret: bytes | None = field(repr=False)
    secret_format: str | None = None


@dataclass(frozen=True, slots=True)
class ModelRecord:
    id: str
    revision: int
    settings: ModelSettings


class ProviderStore:
    """Owned by HostStore; every CAS changes config and ciphertext atomically."""

    def __init__(self, db: sqlite3.Connection) -> None:
        self._db = db

    def providers(self) -> list[ProviderRecord]:
        return [
            self.provider(row[0])
            for row in self._db.execute("SELECT id FROM providers ORDER BY id")
        ]

    def provider(self, identity: str) -> ProviderRecord:
        row = self._db.execute("SELECT * FROM providers WHERE id=?", (identity,)).fetchone()
        if row is None:
            raise NotFoundError("Provider not found")
        return ProviderRecord(
            row["id"],
            row["revision"],
            ProviderSettings.model_validate_json(row["settings"]),
            row["secret"],
            row["secret_format"],
        )

    def models(self) -> list[ModelRecord]:
        return [self.model(row[0]) for row in self._db.execute("SELECT id FROM models ORDER BY id")]

    def model(self, identity: str) -> ModelRecord:
        row = self._db.execute("SELECT * FROM models WHERE id=?", (identity,)).fetchone()
        if row is None:
            raise NotFoundError("Model not found")
        return ModelRecord(
            row["id"], row["revision"], ModelSettings.model_validate_json(row["settings"])
        )

    def put_provider(
        self,
        identity: str,
        settings: ProviderSettings,
        expected_revision: int,
        secret: bytes | None,
        secret_format: str | None,
    ) -> ProviderRecord:
        try:
            if expected_revision == 0:
                self._db.execute(
                    "INSERT INTO providers VALUES(?,1,?,?,?)",
                    (identity, settings.model_dump_json(), secret, secret_format),
                )
            else:
                result = self._db.execute(
                    "UPDATE providers SET revision=revision+1,settings=?,secret=?,secret_format=? "
                    "WHERE id=? AND revision=?",
                    (
                        settings.model_dump_json(),
                        secret,
                        secret_format,
                        identity,
                        expected_revision,
                    ),
                )
                if result.rowcount != 1:
                    raise ConflictError("Provider revision changed")
        except sqlite3.IntegrityError:
            raise ConflictError("Provider already exists") from None
        return self.provider(identity)

    def delete_provider(self, identity: str, expected_revision: int) -> None:
        if self._db.execute("SELECT 1 FROM models WHERE provider_id=?", (identity,)).fetchone():
            raise ConflictError("Provider is referenced by configured models")
        result = self._db.execute(
            "DELETE FROM providers WHERE id=? AND revision=?", (identity, expected_revision)
        )
        if result.rowcount != 1:
            raise ConflictError("Provider revision changed")

    def put_model(
        self, identity: str, settings: ModelSettings, expected_revision: int
    ) -> ModelRecord:
        try:
            if expected_revision == 0:
                self._db.execute(
                    "INSERT INTO models VALUES(?,?,1,?)",
                    (identity, settings.provider_id, settings.model_dump_json()),
                )
            else:
                result = self._db.execute(
                    "UPDATE models SET revision=revision+1,provider_id=?,settings=? "
                    "WHERE id=? AND revision=?",
                    (settings.provider_id, settings.model_dump_json(), identity, expected_revision),
                )
                if result.rowcount != 1:
                    raise ConflictError("Model revision changed")
        except sqlite3.IntegrityError:
            raise ConflictError("Model already exists or its provider is missing") from None
        return self.model(identity)

    def delete_model(self, identity: str, expected_revision: int) -> None:
        result = self._db.execute(
            "DELETE FROM models WHERE id=? AND revision=?", (identity, expected_revision)
        )
        if result.rowcount != 1:
            raise ConflictError("Model revision changed")
